#!/usr/bin/env python3
"""
Morpho liquidation engine
=========================

Deep-dive on liquidations for a curator's Morpho markets. Built as a set of modules
that share one market context (market data, borrowers, DEX price curve):

  1. PROFITABILITY  (built)  - would a rational atomic liquidator actually execute?
                               bonus - oracle/DEX gap - swap cost - gas = net profit,
                               then a 60% MEV bribe. Minimum / optimal / maximum
                               profitable size, and how much of the liquidatable debt
                               is economically liquidatable under each price shock.
  2. Liquidity degradation   (planned)
  3. Concentration & clustering (planned)
  4. Oracle vs executable price divergence (planned)
  5. Bad debt & residual debt simulator (planned)
  6. Liquidation throughput  (planned)
  7. Cross-market contagion  (planned)

Liquidator model (baseline): an atomic on-chain bot. Morpho sends the collateral first and
pulls the repayment at the end of the same transaction (onMorphoLiquidate callback), so the
bot swaps collateral -> loan token inside the transaction and needs no capital or flash loan.
Financing cost is therefore zero.

Usage (keep next to morpho_scan.py and risk_engine.py):
    python3 liquidation_engine.py                      # every market of every curator
    python3 liquidation_engine.py --curator sentora    # one curator

Output: liq_out/<timestamp>/index.html (all curators) and liq_out/<timestamp>/<curator>/report.html + CSVs
"""

import argparse, csv, json, math, os, statistics, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

try:
    import morpho_scan as ms
    import risk_engine as rk
except ImportError:
    sys.exit("liquidation_engine.py needs morpho_scan.py and risk_engine.py in the same folder")

num, fmt_usd, pct, short, lif = rk.num, rk.fmt_usd, rk.pct, rk.short, rk.lif

# ---------------------------------------------------------------------------
# Assumptions (all overridable from the command line)
# ---------------------------------------------------------------------------
MEV_BRIBE_SHARE = 0.60        # share of net profit a liquidator pays block builders to win the race
LIQ_CALL_GAS = 200_000        # gas for Morpho liquidate() + callback overhead, on top of the swap's own gas
STRESS_GAS_MULT = 10          # crash scenario: gas price x10 ...
STRESS_GAS_FLOOR_GWEI = 50    # ... and at least this on Ethereum mainnet
QUOTE_SIZES = [10e3, 100e3, 300e3, 1e6, 3e6, 10e6, 30e6, 100e6]   # USD of collateral sold
GRID_POINTS = 400             # resolution of the profit curve
MIN_SIZE_USD = 100            # smallest trade considered
WETH = {1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 8453: "0x4200000000000000000000000000000000000006",
        42161: "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 10: "0x4200000000000000000000000000000000000006",
        130: "0x4200000000000000000000000000000000000006"}
DEFAULT_GAS_GWEI = {1: 2.0, 8453: 0.01, 42161: 0.02, 10: 0.01, 130: 0.01, 137: 50.0}

RAW = rk.RAW

# ---------------------------------------------------------------------------
# DEX quotes with gas data (KyberSwap)
# ---------------------------------------------------------------------------
_qcache = {}

def kyber_route(chain, token_in, token_out, amount_in_base):
    """routeSummary dict from KyberSwap (amountOut, gas, gasPrice, gasUsd...), or None + status"""
    slug = rk.KYBER_CHAINS.get(chain)
    if not slug:
        return None, "unsupported"
    amount_in_base = int(amount_in_base)
    if amount_in_base <= 0:
        return None, "error"
    key = (chain, token_in.lower(), token_out.lower(), amount_in_base)
    if key in _qcache:
        return _qcache[key]
    url = (f"https://aggregator-api.kyberswap.com/{slug}/api/v1/routes?tokenIn={token_in}"
           f"&tokenOut={token_out}&amountIn={amount_in_base}")
    out = (None, "error")
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"x-client-id": "morpho-liquidation-engine",
                                                   "user-agent": "morpho-liquidation-engine/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            rs = (d.get("data") or {}).get("routeSummary") or {}
            amt = rs.get("amountOut")
            out = (rs, "ok") if amt and int(amt) > 0 else (None, "no_route")
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")[:300]
            if e.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            out = (None, "no_route" if e.code in (400, 404) or "route" in body.lower() else "error")
            break
        except Exception:
            if attempt < 2:
                time.sleep(2); continue
            out = (None, "error")
    time.sleep(0.5)
    _qcache[key] = out
    RAW.setdefault("quotes", []).append({"chain": chain, "in": token_in, "out": token_out,
                                         "amount_in": str(amount_in_base), "status": out[1],
                                         "summary": {k: v for k, v in (out[0] or {}).items() if k != "route"}})
    return out

# ---------------------------------------------------------------------------
# Gas
# ---------------------------------------------------------------------------
_eth_price = {}

def eth_usd(chain):
    if chain in _eth_price:
        return _eth_price[chain]
    p = None
    addr = WETH.get(chain, WETH[1])
    try:
        d = rk.gql('{ assetByAddress(address: "%s", chainId: %d) { price { usd } } }'
                   % (addr, chain if chain in WETH else 1)).get("assetByAddress") or {}
        p = num((d.get("price") or {}).get("usd")) or None
    except Exception:
        pass
    _eth_price[chain] = p or 3000.0
    return _eth_price[chain]

def gas_model(chain, quotes, args):
    """gas price, units and USD cost per liquidation, live and stressed.
    Kyber returns the swap's gas units and current gas price; we add the Morpho call on top."""
    units = [num(q.get("gas")) for q in quotes if q.get("gas")]
    prices = [num(q.get("gasPrice")) for q in quotes if q.get("gasPrice")]
    swap_units = statistics.median(units) if units else 250_000
    gwei = (statistics.median(prices) / 1e9) if prices else DEFAULT_GAS_GWEI.get(chain, 2.0)
    # ETH price implied by Kyber's own gasUsd, otherwise from the Morpho API
    implied = [num(q["gasUsd"]) / (num(q["gas"]) * num(q["gasPrice"]) / 1e18)
               for q in quotes if q.get("gasUsd") and q.get("gas") and q.get("gasPrice")
               and num(q["gas"]) * num(q["gasPrice"]) > 0]
    ethp = statistics.median(implied) if implied else eth_usd(chain)
    total_units = swap_units + args.liq_call_gas
    stress_gwei = gwei * args.stress_gas_mult
    if chain == 1:
        stress_gwei = max(stress_gwei, args.stress_gas_floor)
    usd = lambda g: total_units * g * 1e-9 * ethp
    return {"units": total_units, "swap_units": swap_units, "gwei": gwei, "stress_gwei": stress_gwei,
            "eth_usd": ethp, "live_usd": usd(gwei), "stress_usd": usd(stress_gwei)}

# ---------------------------------------------------------------------------
# Market context: data every module needs, fetched once
# ---------------------------------------------------------------------------
def price_curve(m, chain, op, cu):
    """executable price / oracle price for selling V USD of collateral into the loan token"""
    ca, la = m["collateralAsset"], m["loanAsset"]
    cd, ld = int(num(ca["decimals"])), int(num(la["decimals"]))
    L = lif(num(m["lltv"]) / 1e18)
    pts, quotes, status = [], [], "ok"
    for usd in QUOTE_SIZES:
        qty = usd / cu
        rs, st = kyber_route(chain, ca["address"], la["address"], qty * 10 ** cd)
        if rs is None:
            status = st
            break
        quotes.append(rs)
        q = (int(rs["amountOut"]) / 10 ** ld / qty) / op
        pts.append((usd, q))
        if q < 1 / L - 0.03:          # well past break-even: larger sizes only get worse
            break
    return pts, quotes, status

def q_at(pts, v):
    """interpolate executable/oracle ratio at size v (USD), linear in log(size)"""
    if not pts:
        return None
    if v <= pts[0][0]:
        return pts[0][1]
    for (s0, q0), (s1, q1) in zip(pts, pts[1:]):
        if v <= s1:
            w = (math.log(v) - math.log(s0)) / (math.log(s1) - math.log(s0))
            return q0 + w * (q1 - q0)
    return None                       # beyond the largest size we have a quote for

def build_context(m, chain, borrowers, exposure, vault_share, args):
    op = rk.oracle_price(m)
    cu = rk.coll_usd(m, op)
    lu = rk.loan_usd(m)
    lltv = num(m["lltv"]) / 1e18
    ld = int(num(m["loanAsset"]["decimals"])); cd = int(num(m["collateralAsset"]["decimals"]))
    pos = []
    for b in borrowers:
        st = b.get("state") or {}
        debt = num(st.get("borrowAssets")) / 10 ** ld
        coll = num(st.get("collateral")) / 10 ** cd
        hf = b.get("healthFactor")
        if hf is None and op and debt:
            hf = coll * op * lltv / debt
        if hf is None or debt <= 0:
            continue
        pos.append({"user": b["user"]["address"], "hf": num(hf), "debt": debt, "coll": coll})
    pts, quotes, status = price_curve(m, chain, op, cu) if op and cu else ([], [], "no price")
    fam_c, fam_l = rk.family(m["collateralAsset"]["symbol"]), rk.family(m["loanAsset"]["symbol"])
    correlated = fam_c is not None and fam_c == fam_l
    return {"m": m, "chain": chain, "label": rk.market_label(m), "exposure": exposure,
            "vault_share": vault_share, "op": op, "coll_usd": cu, "loan_usd": lu, "lltv": lltv,
            "LIF": lif(lltv), "positions": pos, "curve": pts, "curve_status": status,
            "gas": gas_model(chain, quotes, args) if op else None,
            "oracle": rk.classify_oracle(m), "pre_liquidations": fetch_pre_liquidations(m["marketId"], chain),
            "shocks": [0.02, 0.05, 0.10] if correlated else [0.10, 0.20, 0.30],
            "total_borrow_usd": num(m["state"]["borrowAssets"]) / 10 ** ld * lu}

_pre_sel = None

def fetch_pre_liquidations(mid, chain):
    """Morpho pre-liquidation contracts on this market (borrowers opt in to earlier, partial liquidations)"""
    global _pre_sel
    if _pre_sel is None:
        t = rk._type("Market")
        f = [x for x in (t or {}).get("fields") or [] if x["name"] == "preLiquidations"]
        _pre_sel = ""
        if f:
            tname, _ = rk._unwrap(f[0]["type"])
            _pre_sel = rk._selection(tname) or ""
            args = f[0].get("args") or []
            if any(a["name"] == "first" for a in args):
                _pre_sel = ("(first: 20)", _pre_sel)
    if not _pre_sel:
        return None
    arg, sel = _pre_sel if isinstance(_pre_sel, tuple) else ("", _pre_sel)
    if not sel:
        return None
    try:
        d = rk.gql('{ marketById(marketId: "%s", chainId: %d) { preLiquidations%s { %s } } }'
                   % (mid, chain, arg, sel)).get("marketById") or {}
    except Exception:
        return None
    pl = d.get("preLiquidations")
    RAW.setdefault("pre_liquidations", {})[mid] = pl
    items = pl.get("items") if isinstance(pl, dict) else pl
    items = [i for i in (items or []) if i]
    out = []
    for it in items:
        flat = {}
        def walk(o, p=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, f"{p}.{k}" if p else k)
            elif not isinstance(o, list):
                flat[p] = o
        walk(it)
        params = {k.split(".")[-1]: v for k, v in flat.items()
                  if any(s in k.lower() for s in ("lltv", "lcf", "lif", "address"))}
        out.append(params)
    return out

# ---------------------------------------------------------------------------
# Module 1: liquidator profitability
# ---------------------------------------------------------------------------
def profit_curve(ctx, gas_usd):
    """net profit (before MEV) as a function of seized collateral value V (USD).
    repaid r = V / LIF ; proceeds = V * q(V) ; net = proceeds - r - gas"""
    pts, L = ctx["curve"], ctx["LIF"]
    if not pts:
        return []
    hi = pts[-1][0]
    grid = [MIN_SIZE_USD * (hi / MIN_SIZE_USD) ** (i / (GRID_POINTS - 1)) for i in range(GRID_POINTS)]
    out = []
    for v in grid:
        q = q_at(pts, v)
        if q is None:
            continue
        out.append((v, v * q - v / L - gas_usd))
    return out

def window(curve):
    """(min profitable V, optimal V, max profitable V, best profit)"""
    ok = [(v, p) for v, p in curve if p >= 0]
    if not ok:
        return None, None, None, max((p for _, p in curve), default=None)
    vmin = ok[0][0]
    # max: last profitable point in the run that starts at vmin
    vmax = vmin
    started = False
    for v, p in curve:
        if v >= vmin and p >= 0:
            vmax, started = v, True
        elif started:
            break
    vbest, pbest = max(ok, key=lambda x: x[1])
    return vmin, vbest, vmax, pbest

def breakdown(ctx, v, gas_usd):
    """split net profit at seized value v into bonus, oracle gap, swap cost and gas"""
    L, pts = ctx["LIF"], ctx["curve"]
    q_mid, q_v = pts[0][1], q_at(pts, v)
    r = v / L
    bonus = v - r
    gap = v * (1 - q_mid)               # oracle vs DEX price at small size (includes pool fees)
    swap = v * (q_mid - q_v)            # extra slippage from trading size v
    net = bonus - gap - swap - gas_usd
    return {"seized": v, "repaid": r, "bonus": bonus, "gap": gap, "swap": swap, "gas": gas_usd,
            "net": net, "margin": net / r if r else None,
            "bribe": max(0.0, net) * MEV_BRIBE_SHARE, "take": max(0.0, net) * (1 - MEV_BRIBE_SHARE)}

def module_profitability(ctx):
    res = {"bonus": ctx["LIF"] - 1, "curve_status": ctx["curve_status"], "scenarios": {}}
    if not ctx["curve"] or not ctx["gas"]:
        res["flag"] = "grey"
        res["note"] = ("no DEX route for the collateral: on-chain liquidators cannot act"
                       if ctx["curve_status"] == "no_route" else "no price data")
        if ctx["curve_status"] == "no_route":
            res["flag"] = "red"
        return res
    res["mid_ratio"] = ctx["curve"][0][1]
    res["quoted_up_to"] = ctx["curve"][-1][0]
    for name, gas_usd in (("live", ctx["gas"]["live_usd"]), ("stress", ctx["gas"]["stress_usd"])):
        curve = profit_curve(ctx, gas_usd)
        vmin, vbest, vmax, pbest = window(curve)
        sc = {"gas_usd": gas_usd, "vmin": vmin, "vbest": vbest, "vmax": vmax,
              "min_repay": vmin / ctx["LIF"] if vmin else None,
              "best_repay": vbest / ctx["LIF"] if vbest else None,
              "max_repay": vmax / ctx["LIF"] if vmax else None,
              "best": breakdown(ctx, vbest, gas_usd) if vbest else None,
              "at_1m": breakdown(ctx, 1e6 * ctx["LIF"], gas_usd)
                       if q_at(ctx["curve"], 1e6 * ctx["LIF"]) is not None else None,
              # if the largest quote was still profitable, the true max is above what we measured
              "max_is_floor": bool(vmax and vmax >= ctx["curve"][-1][0] * 0.99)}
        res["scenarios"][name] = sc
    # price shocks: who becomes liquidatable, and how much of it a rational bot would clear
    res["shocks"] = []
    L, op, lu = ctx["LIF"], ctx["op"], ctx["loan_usd"]
    for s in ctx["shocks"]:
        rows = []
        for p in ctx["positions"]:
            if p["hf"] * (1 - s) >= 1:
                continue
            debt_usd = p["debt"] * lu
            coll_usd = p["coll"] * op * (1 - s) * lu
            v_full = min(debt_usd * L, coll_usd)          # all collateral if the position is underwater
            residual = max(0.0, debt_usd - v_full / L)    # debt left even after seizing everything
            rows.append({"user": p["user"], "hf_after": p["hf"] * (1 - s), "debt": debt_usd,
                         "coll_after": coll_usd, "v_full": v_full, "repayable": v_full / L,
                         "residual": residual})
        sh = {"shock": s, "n": len(rows), "debt": sum(r["debt"] for r in rows),
              "residual": sum(r["residual"] for r in rows), "positions": rows}
        for name in ("live", "stress"):
            sc = res["scenarios"][name]
            vmin, vmax = sc["vmin"], sc["vmax"]
            if vmin is None:
                prof, dust_n, dust_usd, block = 0.0, len(rows), sh["debt"], 0.0
            else:
                ok = [r for r in rows if r["v_full"] >= vmin]
                prof = sum(r["repayable"] for r in ok)
                dust = [r for r in rows if r["v_full"] < vmin]
                dust_n, dust_usd = len(dust), sum(r["debt"] for r in dust)
                block = min(sum(r["v_full"] for r in rows), vmax) / L
            sh[name] = {"profitable_debt": prof,
                        "profitable_pct": prof / sh["debt"] if sh["debt"] else None,
                        "one_block_debt": block,
                        "one_block_pct": block / sh["debt"] if sh["debt"] else None,
                        "dust_n": dust_n, "dust_usd": dust_usd}
            for r in rows:
                r[f"profitable_{name}"] = vmin is not None and r["v_full"] >= vmin
        res["shocks"].append(sh)
    # flag on the middle shock, stressed gas
    mid = res["shocks"][1]
    one = (mid["stress"]["one_block_pct"] if mid["debt"] else None)
    if res["scenarios"]["stress"]["vmin"] is None:
        res["flag"] = "red"            # no profitable liquidation at any size
    elif mid["debt"] and one is not None and one < 0.25:
        res["flag"] = "red"            # under a quarter of the shocked debt clears profitably in one block
    elif mid["debt"] and one is not None and one < 1.0:
        res["flag"] = "amber"
    else:
        res["flag"] = "green"
    return res

MODULES = {"profitability": module_profitability}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
FLAG = rk.FLAG

def fmt_ratio(q):
    return "-" if q is None else f"{(q - 1) * 100:+.2f}%"

def write_report(name, ctxs, outdir, args, material):
    L = [f"# {name} — Morpho liquidation engine", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · {len(ctxs)} markets · module: liquidator profitability", "",
         "**Question:** when a position becomes liquidatable, would a rational liquidator actually do it? "
         "Baseline liquidator: an atomic on-chain bot. Morpho hands over the collateral first and collects the "
         "repayment at the end of the same transaction, so the bot swaps the collateral inside the transaction "
         "and needs no capital or flash loan (financing cost = 0).", "",
         f"**Assumptions:** gas = KyberSwap's swap estimate + {args.liq_call_gas:,} for the Morpho call; stressed gas = "
         f"{args.stress_gas_mult}× live (at least {args.stress_gas_floor} gwei on Ethereum). MEV: a liquidation happens "
         f"if profit before MEV is positive; competing bots then pay ~{MEV_BRIBE_SHARE:.0%} of it to block builders. "
         "Price shocks move the oracle and the DEX curve together (slippage in % unchanged) — a best case; "
         "liquidity degradation comes in module 2.", ""]
    # key findings
    F = []
    for c in ctxs:
        r = c.get("profitability") or {}
        if c["exposure"] < material:
            continue
        if r.get("flag") == "red":
            if r.get("note"):
                F.append(("red", c["exposure"], f"**{c['label']}**: {r['note']}"))
            elif r["scenarios"]["stress"]["vmin"] is None:
                F.append(("red", c["exposure"], f"**{c['label']}**: no liquidation size is profitable "
                          f"(DEX price {fmt_ratio(r['mid_ratio'])} vs oracle against a {pct(r['bonus'],2)} bonus)"))
            else:
                sh = r["shocks"][1]; st = r["scenarios"]["stress"]
                F.append(("red", c["exposure"],
                          f"**{c['label']}**: at −{sh['shock']*100:.0f}%, {fmt_usd(sh['debt'])} becomes liquidatable but a "
                          f"profitable single block clears only {fmt_usd(sh['stress']['one_block_debt'])} "
                          f"({pct(sh['stress']['one_block_pct'],0)}); the largest profitable trade repays "
                          f"{fmt_usd(st['max_repay'])}."))
        elif r.get("flag") == "amber":
            sh = r["shocks"][1]
            F.append(("amber", c["exposure"],
                      f"**{c['label']}**: at −{sh['shock']*100:.0f}%, {pct(sh['stress']['one_block_pct'],0)} of "
                      f"{fmt_usd(sh['debt'])} liquidatable debt clears profitably in one block"))
        for sh in r.get("shocks") or []:
            if sh["stress"]["dust_n"] and sh["stress"]["dust_usd"] >= 10_000:
                F.append(("amber", c["exposure"],
                          f"**{c['label']}**: at −{sh['shock']*100:.0f}% with crash gas, {sh['stress']['dust_n']} positions "
                          f"({fmt_usd(sh['stress']['dust_usd'])}) are too small to liquidate at a profit"))
                break
    order = {"red": 0, "amber": 1}
    F.sort(key=lambda x: (order[x[0]], -x[1]))
    L += ["## Key findings", ""] + ([f"- {FLAG[s]} · {t}" for s, _, t in F] or ["- none"]) + [""]

    # table 1: economics today
    L += ["## 1. Liquidator economics at today's prices", "",
          "Repay = debt the liquidator repays in one trade. **Min** = smallest trade where the bonus covers gas. "
          "**Optimal** = trade size with the highest profit. **Max** = largest trade still profitable before slippage "
          "eats the bonus ('≥' means it was still profitable at the largest size quoted). DEX vs oracle = KyberSwap "
          "price for $10K of collateral relative to the oracle (includes pool fees).", "",
          "| Market | Exposure | Bonus | DEX vs oracle | Gas live | Gas crash | Min repay (live) | Min repay (crash) | "
          "Optimal repay (crash gas) | Profit at optimal (pre-MEV) | Margin | Liquidator take after MEV | Max repay | Flag |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in ctxs:
        r = c.get("profitability") or {}
        if not r.get("scenarios"):
            L.append(f"| {c['label']} | {fmt_usd(c['exposure'])} | {pct(r.get('bonus'),2)} | {r.get('note','-')} |"
                     " | | | | | | | | | " + FLAG.get(r.get("flag"), "-") + " |")
            continue
        lv, st = r["scenarios"]["live"], r["scenarios"]["stress"]
        b = st["best"] or {}
        mx = ("≥" if st["max_is_floor"] else "") + fmt_usd(st["max_repay"]) if st["max_repay"] else "none"
        L.append("| " + " | ".join([
            c["label"], fmt_usd(c["exposure"]), pct(r["bonus"], 2), fmt_ratio(r["mid_ratio"]),
            f"${lv['gas_usd']:,.2f}", f"${st['gas_usd']:,.0f}",
            fmt_usd(lv["min_repay"]) if lv["min_repay"] else "none",
            fmt_usd(st["min_repay"]) if st["min_repay"] else "none",
            fmt_usd(st["best_repay"]) if st["best_repay"] else "-",
            fmt_usd(b.get("net")), pct(b.get("margin"), 2), fmt_usd(b.get("take")), mx,
            FLAG[r["flag"]]]) + " |")
    L.append("")

    # table 2: waterfall at optimal size
    L += ["## 2. Where the money goes (optimal trade, crash gas)", "",
          "Net profit = bonus − oracle/DEX gap − swap slippage − gas. Bribe = MEV paid to win the race "
          f"({MEV_BRIBE_SHARE:.0%} of net profit); the liquidator keeps the rest. Margin = net profit ÷ debt repaid.", "",
          "| Market | Debt repaid | Collateral seized | Bonus | − Oracle/DEX gap | − Swap slippage | − Gas | = Net profit | "
          "Margin | − MEV bribe | = Liquidator take | Margin at $1M repaid |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in ctxs:
        r = c.get("profitability") or {}
        st = (r.get("scenarios") or {}).get("stress") or {}
        b = st.get("best")
        if not b:
            continue
        m1 = st.get("at_1m")
        L.append("| " + " | ".join([
            c["label"], fmt_usd(b["repaid"]), fmt_usd(b["seized"]), fmt_usd(b["bonus"]), fmt_usd(b["gap"]),
            fmt_usd(b["swap"]), fmt_usd(b["gas"]), fmt_usd(b["net"]), pct(b["margin"], 2),
            fmt_usd(b["bribe"]), fmt_usd(b["take"]),
            pct(m1["margin"], 2) if m1 else "beyond quotes"]) + " |")
    L.append("")

    # table 3: shocks
    L += ["## 3. Under price shocks: how much liquidatable debt is worth liquidating?", "",
          "**Liquidatable debt** = debt of borrowers whose health factor falls below 1. **Profitable** = debt in positions "
          "big enough for the bonus to cover gas (each position on its own, fresh liquidity). **One block** = how much "
          "can be cleared at a profit right away, when all liquidations share the same DEX liquidity (capped by the max "
          "profitable trade). **Dust** = positions too small to be worth liquidating. **Underwater** = debt left even if "
          "all collateral is seized (becomes bad debt; module 5).", "",
          "| Market | Shock | Positions | Liquidatable debt | Underwater | Profitable (live gas) | Profitable (crash gas) | "
          "One block (crash gas) | Dust (crash gas) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for c in ctxs:
        r = c.get("profitability") or {}
        for i, sh in enumerate(r.get("shocks") or []):
            L.append("| " + " | ".join([
                c["label"] if i == 0 else "", f"−{sh['shock']*100:.0f}%", str(sh["n"]), fmt_usd(sh["debt"]),
                fmt_usd(sh["residual"]),
                pct(sh["live"]["profitable_pct"], 0) if sh["debt"] else "-",
                pct(sh["stress"]["profitable_pct"], 0) if sh["debt"] else "-",
                (f"{fmt_usd(sh['stress']['one_block_debt'])} ({pct(sh['stress']['one_block_pct'],0)})") if sh["debt"] else "-",
                (f"{sh['stress']['dust_n']} · {fmt_usd(sh['stress']['dust_usd'])}") if sh["stress"]["dust_n"] else "0"]) + " |")
    L.append("")

    # table 4: setup facts
    L += ["## 4. Market setup", "",
          "Oracle kind matters: if the oracle reads only an exchange rate, a market crash may not trigger liquidations "
          "at all. Pre-liquidation contracts let opted-in borrowers be liquidated earlier and partially, on their own terms.", "",
          "| Market | LLTV | Oracle | Pre-liquidation contracts | Gas units | Gas price live → crash | ETH price used | Largest quote |",
          "|---|---|---|---|---|---|---|---|"]
    for c in ctxs:
        g = c.get("gas") or {}
        pl = c.get("pre_liquidations")
        pltxt = "unknown" if pl is None else (str(len(pl)) + (": " + "; ".join(
            ", ".join(f"{k}={v}" for k, v in list(p.items())[:4]) for p in pl[:2]) if pl else ""))
        L.append("| " + " | ".join([
            c["label"], pct(c["lltv"], 1), f"{c['oracle'].get('kind')} ({c['oracle'].get('source')})", pltxt,
            f"{g.get('units', 0):,.0f}" if g else "-",
            f"{g['gwei']:.2f} → {g['stress_gwei']:.0f} gwei" if g else "-",
            fmt_usd(g.get("eth_usd")) if g else "-",
            fmt_usd((c.get("profitability") or {}).get("quoted_up_to"))]) + " |")
    L.append("")
    L += ["## Method", "",
          "- Profit curve: KyberSwap quotes for selling $10K → $100M of collateral into the loan token; executable price ÷ "
          "oracle price is interpolated between quote sizes (log scale). For seized collateral V: repaid = V ÷ LIF, "
          "proceeds = V × executable/oracle, net = proceeds − repaid − gas.",
          "- Bonus = V − V ÷ LIF. Oracle/DEX gap = V × (1 − small-trade price/oracle). Swap slippage = V × (small-trade "
          "price − size price)/oracle. These add up exactly to net profit.",
          "- Underwater positions: if collateral after the shock is worth less than debt × LIF, the liquidator seizes "
          "all of it and repays collateral ÷ LIF; the rest stays as residual debt.",
          "- Borrowers are assumed not to add collateral; the 100 largest positions are read."]
    md = "\n".join(L)
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    html = ms.md_to_html(md, f"{name} liquidation engine").replace("max-width:1200px", "max-width:1800px")
    for word, col in (("RED", "#c0392b"), ("AMBER", "#b9770e"), ("OK", "#1e8449"), ("INFO", "#2f5d8a")):
        html = html.replace(f"<td>{word}</td>", f'<td><b style="color:{col}">{word}</b></td>')
        html = html.replace(f"<li>{word} · ", f'<li><b style="color:{col}">{word}</b> · ')
    page = os.path.join(outdir, "report.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    return page

def write_csvs(ctxs, outdir):
    with open(os.path.join(outdir, "profitability.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["market", "market_id", "chain", "exposure_usd", "lltv", "bonus", "dex_vs_oracle",
                    "gas_units", "gas_gwei_live", "gas_gwei_crash", "gas_usd_live", "gas_usd_crash",
                    "min_repay_live", "min_repay_crash", "optimal_repay_crash", "max_repay_crash", "max_is_floor",
                    "net_profit_optimal", "margin_optimal", "bonus_usd", "gap_usd", "swap_usd", "bribe_usd",
                    "liquidator_take_usd", "flag"])
        for c in ctxs:
            r = c.get("profitability") or {}
            lv = (r.get("scenarios") or {}).get("live") or {}
            st = (r.get("scenarios") or {}).get("stress") or {}
            b = st.get("best") or {}
            g = c.get("gas") or {}
            w.writerow([c["label"], c["m"]["marketId"], c["chain"], round(c["exposure"]), c["lltv"], r.get("bonus"),
                        (r.get("mid_ratio") or 1) - 1 if r.get("mid_ratio") else None,
                        g.get("units"), g.get("gwei"), g.get("stress_gwei"), g.get("live_usd"), g.get("stress_usd"),
                        lv.get("min_repay"), st.get("min_repay"), st.get("best_repay"), st.get("max_repay"),
                        st.get("max_is_floor"), b.get("net"), b.get("margin"), b.get("bonus"), b.get("gap"),
                        b.get("swap"), b.get("bribe"), b.get("take"), r.get("flag")])
    with open(os.path.join(outdir, "shocks.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["market", "shock", "positions", "liquidatable_debt_usd", "underwater_usd",
                    "profitable_debt_live", "profitable_pct_live", "profitable_debt_crash", "profitable_pct_crash",
                    "one_block_debt_crash", "one_block_pct_crash", "dust_n_crash", "dust_usd_crash"])
        for c in ctxs:
            for sh in (c.get("profitability") or {}).get("shocks") or []:
                w.writerow([c["label"], sh["shock"], sh["n"], sh["debt"], sh["residual"],
                            sh["live"]["profitable_debt"], sh["live"]["profitable_pct"],
                            sh["stress"]["profitable_debt"], sh["stress"]["profitable_pct"],
                            sh["stress"]["one_block_debt"], sh["stress"]["one_block_pct"],
                            sh["stress"]["dust_n"], sh["stress"]["dust_usd"]])
    with open(os.path.join(outdir, "positions.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["market", "shock", "borrower", "hf_after_shock", "debt_usd", "collateral_after_usd",
                    "repayable_usd", "underwater_usd", "profitable_live_gas", "profitable_crash_gas"])
        for c in ctxs:
            for sh in (c.get("profitability") or {}).get("shocks") or []:
                for p in sh["positions"]:
                    w.writerow([c["label"], sh["shock"], p["user"], round(p["hf_after"], 4), round(p["debt"]),
                                round(p["coll_after"]), round(p["repayable"]), round(p["residual"]),
                                p.get("profitable_live"), p.get("profitable_stress")])
    with open(os.path.join(outdir, "price_curves.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["market", "sell_usd", "executable_over_oracle"])
        for c in ctxs:
            for v, q in c["curve"]:
                w.writerow([c["label"], v, q])

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def collect_markets(curator_key, targets):
    """{(chain, market_id): {exposure, vault_share}} across all of a curator's vaults"""
    out = {}
    for t in [t for t in targets if t["curator"] == curator_key]:
        print(f"   {t['name']} ({fmt_usd(t['tvl'])})")
        try:
            v = rk.fetch_vault(t)
        except Exception as e:
            print(f"      skipped: {e}"); continue
        for mk in v["markets"]:
            exp = mk["x"] * v["price"]
            if mk["S"] <= 0 or exp <= 0:
                continue
            k = (t["chain"], mk["market_id"].lower())
            e = out.setdefault(k, {"exposure": 0.0, "vault_share": 0.0})
            e["exposure"] += exp
            e["vault_share"] = min(1.0, e["vault_share"] + mk["x"] / mk["S"])
    return out

_ctx_cache = {}

def market_context(chain, mid, info, args):
    """market data, borrowers, price curve and module results; computed once per market and
    reused when several curators lend to the same market (only exposure differs)"""
    key = (chain, mid)
    if key not in _ctx_cache:
        m = rk.fetch_market(mid, chain)
        if not m or not m.get("collateralAsset"):
            _ctx_cache[key] = None
        else:
            ctx = build_context(m, chain, rk.fetch_borrowers(mid, args.borrowers), 0.0, 0.0, args)
            for mod_name, fn in MODULES.items():
                ctx[mod_name] = fn(ctx)
            _ctx_cache[key] = ctx
    base = _ctx_cache[key]
    if base is None:
        return None
    ctx = dict(base)
    ctx["exposure"], ctx["vault_share"] = info["exposure"], info["vault_share"]
    return ctx

def run_curator(ck, targets, args, stamp):
    label = rk.CURATORS[ck]["label"]
    print(f"\n=== {label} ===")
    markets = collect_markets(ck, targets)
    markets = {k: v for k, v in markets.items() if v["exposure"] >= args.min_exposure}
    if not markets:
        print("   no markets found - check CURATORS keywords, --chains or --min-tvl")
        return None
    total = sum(v["exposure"] for v in markets.values())
    material = max(5_000_000, 0.005 * total)
    print(f"   analysing {len(markets)} markets ({fmt_usd(total)} lent)...")
    ctxs = []
    for i, ((chain, mid), info) in enumerate(sorted(markets.items(), key=lambda kv: -kv[1]["exposure"]), 1):
        try:
            ctx = market_context(chain, mid, info, args)
            if ctx is None:
                print(f"   ({i}/{len(markets)}) {mid[:10]} skipped (no market data)"); continue
            print(f"   ({i}/{len(markets)}) {ctx['label']}  {fmt_usd(info['exposure'])}")
            ctxs.append(ctx)
        except Exception as e:
            print(f"      {mid[:10]} failed: {type(e).__name__}: {e}")
    outdir = os.path.join("liq_out", stamp, ck)
    os.makedirs(outdir, exist_ok=True)
    page = write_report(label, ctxs, outdir, args, material)
    write_csvs(ctxs, outdir)
    flags = [(c.get("profitability") or {}).get("flag") for c in ctxs]
    return {"curator": label, "page": os.path.join(ck, "report.html"), "markets": len(ctxs), "lent": total,
            "red": flags.count("red"), "amber": flags.count("amber"),
            "red_usd": sum(c["exposure"] for c in ctxs if (c.get("profitability") or {}).get("flag") == "red")}

def write_index(rows, outdir):
    L = ["# Morpho liquidation engine — all curators", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · module: liquidator profitability", "",
         "| Curator | Markets | Lent | RED markets | Lent in RED markets | AMBER markets | Report |",
         "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["lent"]):
        L.append(f"| {r['curator']} | {r['markets']} | {fmt_usd(r['lent'])} | {r['red']} | {fmt_usd(r['red_usd'])} | "
                 f"{r['amber']} | {r['page']} |")
    md = "\n".join(L)
    html = ms.md_to_html(md, "Liquidation engine - all curators")
    for r in rows:     # make report paths clickable
        html = html.replace(f"<td>{r['page']}</td>", f'<td><a href="{r["page"]}">open</a></td>')
    page = os.path.join(outdir, "index.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    return page

def main():
    ap = argparse.ArgumentParser(description="Morpho liquidation engine")
    ap.add_argument("--curator", default="all", help="curator key or 'all' (default): " + ", ".join(rk.CURATORS))
    ap.add_argument("--chains", type=int, nargs="+", default=[1, 8453, 42161, 130, 137])
    ap.add_argument("--min-tvl", type=float, default=1_000_000, help="vaults smaller than this are skipped")
    ap.add_argument("--min-exposure", type=float, default=0,
                    help="skip markets where the curator lends less than this (default: every market)")
    ap.add_argument("--borrowers", type=int, default=100)
    ap.add_argument("--mev-share", type=float, default=MEV_BRIBE_SHARE)
    ap.add_argument("--liq-call-gas", type=int, default=LIQ_CALL_GAS)
    ap.add_argument("--stress-gas-mult", type=float, default=STRESS_GAS_MULT)
    ap.add_argument("--stress-gas-floor", type=float, default=STRESS_GAS_FLOOR_GWEI, help="gwei, Ethereum only")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    globals()["MEV_BRIBE_SHARE"] = args.mev_share
    keys = list(rk.CURATORS) if args.curator.lower() == "all" else [args.curator.lower()]
    for k in keys:
        if k not in rk.CURATORS:
            sys.exit(f"unknown curator '{k}'. Options: all, {', '.join(rk.CURATORS)}")
    print("Finding vaults...")
    targets, _ = rk.discover(keys, args.chains, args.min_tvl)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    rows = [r for r in (run_curator(k, targets, args, stamp) for k in keys) if r]
    outdir = os.path.join("liq_out", stamp)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "raw.json"), "w", encoding="utf-8") as f:
        json.dump(RAW, f)
    page = write_index(rows, outdir) if len(keys) > 1 else (os.path.join(outdir, rows[0]["page"]) if rows else None)
    print(f"\n-> {page}")
    if page and not args.no_open:
        try:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(page))
        except Exception:
            pass

if __name__ == "__main__":
    main()
