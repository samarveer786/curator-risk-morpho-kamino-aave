#!/usr/bin/env python3
"""
Morpho curator risk engine  (v2)
================================

One engine, any curator. For every vault a curator runs it checks:

  1. ORACLE         - what the oracle actually prices (market feed of the collateral,
                      an underlying asset, or just an exchange rate) and how far it sits
                      from the real DEX price. Tells you whether a depeg of the collateral
                      can trigger liquidations at all.
  2. LIQUIDATIONS   - from the actual borrowers: how far prices must fall before a
                      quarter / half of the debt is liquidatable, and whether DEX
                      liquidity can absorb those liquidations inside the liquidation bonus.
                      Capacity is measured twice: selling into the loan token (what an
                      on-chain liquidator faces) and into USDC (shows when the loan
                      token itself, e.g. PYUSD or RLUSD, is the bottleneck).
  3. BORROWERS      - from hourly history: how often utilization spikes above the kink,
                      how long spikes last, and whether they end by borrowers repaying
                      or by new supply arriving.
  4. RUNS           - largest depositors vs. what can be withdrawn right now; who the
                      depositors are (other vaults, borrowers in the same markets, the
                      same wallet across several vaults); how hard the rate curve pushes
                      borrowers out if a vault is drained.

Usage (keep this file next to morpho_scan.py - it reuses its API helpers):
    python3 risk_engine.py --curator sentora
    python3 risk_engine.py --curator all
    python3 risk_engine.py --curator galaxy --modules runs oracle
    python3 risk_engine.py --list-curators

Output: risk_out/<curator>_<timestamp>/report.html (+ CSVs and raw API data)

Data sources: Morpho GraphQL API, KyberSwap aggregator quotes (no key needed).
"""

import argparse, csv, json, os, statistics, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

try:
    import morpho_scan as ms
except ImportError:
    sys.exit("risk_engine.py needs morpho_scan.py in the same folder")

# ---------------------------------------------------------------------------
# Curators: matched on vault name (case-insensitive). Add or edit freely.
# ---------------------------------------------------------------------------
CURATORS = {
    "sentora":   {"label": "Sentora",   "keywords": ["sentora", "paypal usd main"]},
    "steakhouse": {"label": "Steakhouse", "keywords": ["steakhouse", "smokehouse"]},
    "gauntlet":  {"label": "Gauntlet",  "keywords": ["gauntlet"], "exclude": ["not gauntlet"]},
    "rockawayx": {"label": "RockawayX", "keywords": ["rockawayx", "huma usdc main"]},
    "galaxy":    {"label": "Galaxy",    "keywords": ["galaxy"]},
}

KYBER_CHAINS = {1: "ethereum", 8453: "base", 42161: "arbitrum", 137: "polygon",
                130: "unichain", 10: "optimism"}
USDC = {1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        130: "0x078D782b760474a361dDA0AF3839290b0EF57AD6",
        10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"}
REF_QUOTE_USD = 10_000          # size of the "market price" quote
MIN_EXPOSURE_USD = 50_000       # ignore vault positions smaller than this
MATERIAL_USD = 5_000_000        # headline findings need at least this much exposure (or 0.5% of curator TVL)
CAPACITY_STEPS = [100e3, 300e3, 1e6, 3e6, 10e6, 30e6, 100e6]

# rough asset families: shocks are applied to collateral/loan, so a wstETH/WETH
# market needs a "depeg-size" shock, not a "crash-size" one
ETH_LIKE = {"weth", "eth", "steth", "wsteth", "weeth", "reth", "cbeth", "ezeth", "rseth", "oeth",
            "meth", "sweth", "ethx", "pufeth", "wbeth", "oseth", "lseth", "tetheth", "mseth"}
BTC_LIKE = {"wbtc", "cbbtc", "lbtc", "tbtc", "kbtc", "solvbtc", "ebtc", "fbtc", "pumpbtc", "unibtc",
            "btc", "mbtc", "xsolvbtc", "lbtcv"}

def base_symbol(sym):
    s = (sym or "").lower()
    if s.startswith("pt-"):
        s = s[3:].split("-")[0]
    return s

def family(sym):
    s = base_symbol(sym)
    if s in ETH_LIKE or s.endswith("eth"):
        return "ETH"
    if s in BTC_LIKE or s.endswith("btc"):
        return "BTC"
    if "usd" in s or s in {"dai", "gho", "frax", "crvusd", "usde", "susde", "usds", "susds", "prime",
                           "pst", "fxsave", "siusd", "strusd", "mf-one", "syrupusdc", "syrupusdt",
                           "usd3", "reusd", "eurc", "eurcv"}:
        return "USD"
    return None

def lif(lltv):
    """Morpho liquidation incentive factor"""
    return min(1.15, 1 / (0.3 * lltv + 0.7))

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def num(v):
    return float(v) if v is not None else 0.0

def fmt_usd(x):
    if x is None:
        return "-"
    a = abs(x)
    if a >= 1e9: return f"${x/1e9:,.2f}B"
    if a >= 1e6: return f"${x/1e6:,.1f}M"
    if a >= 1e3: return f"${x/1e3:,.0f}K"
    return f"${x:,.0f}"

def fx(x, spec, suffix=""):
    return "-" if x is None else format(x, spec) + suffix

def pct(x, d=1):
    return "-" if x is None else f"{x*100:.{d}f}%"

def short(a):
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else (a or "-")

def trades_txt(t):
    if t is None:
        return "-"
    if t == float("inf"):
        return "no route"
    return f"{t:.1f}"

RAW = {}       # everything fetched, saved for audit

def gql(q, v=None):
    r = ms.gql(q, v)
    return r.get("data") or {}

# ---------------------------------------------------------------------------
# DEX quotes (KyberSwap aggregator)
# ---------------------------------------------------------------------------
_quote_cache = {}

def kyber_quote(chain, token_in, token_out, amount_in_base):
    """returns (amount_out_base or None, status) ; status in ok / no_route / unsupported / error"""
    slug = KYBER_CHAINS.get(chain)
    if not slug:
        return None, "unsupported"
    amount_in_base = int(amount_in_base)
    if amount_in_base <= 0:
        return None, "error"
    key = (chain, token_in.lower(), token_out.lower(), amount_in_base)
    if key in _quote_cache:
        return _quote_cache[key]
    url = (f"https://aggregator-api.kyberswap.com/{slug}/api/v1/routes?tokenIn={token_in}"
           f"&tokenOut={token_out}&amountIn={amount_in_base}")
    out = (None, "error")
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"x-client-id": "morpho-risk-engine",
                                                   "user-agent": "morpho-risk-engine/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            amt = ((d.get("data") or {}).get("routeSummary") or {}).get("amountOut")
            out = (int(amt), "ok") if amt and int(amt) > 0 else (None, "no_route")
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
    _quote_cache[key] = out
    return out

def sell_price(chain, token_in, dec_in, token_out, dec_out, qty):
    """units of token_out received per unit of token_in when selling qty of token_in"""
    out, status = kyber_quote(chain, token_in, token_out, qty * 10 ** dec_in)
    if out is None:
        return None, status
    return (out / 10 ** dec_out) / qty, status

# ---------------------------------------------------------------------------
# Schema introspection (used to read oracle composition without guessing field names)
# ---------------------------------------------------------------------------
TYPE_Q = """query($n: String!) { __type(name: $n) { name kind possibleTypes { name }
  fields { name args { name type { kind } }
           type { kind name ofType { kind name ofType { kind name ofType { kind name } } } } } } }"""
_types = {}

def _type(name):
    if name not in _types:
        try:
            _types[name] = gql(TYPE_Q, {"n": name}).get("__type")
        except Exception:
            _types[name] = None
    return _types[name]

def _unwrap(t):
    while t and t["kind"] in ("NON_NULL", "LIST"):
        t = t["ofType"]
    return (t or {}).get("name"), (t or {}).get("kind")

SKIP_FIELDS = {"markets", "creationEvent", "historicalState", "state", "warnings", "chain", "vault"}

def _selection(type_name, depth=0):
    t = _type(type_name)
    if not t:
        return None
    parts = []
    if t["kind"] in ("UNION", "INTERFACE"):
        parts.append("__typename")
        for p in t.get("possibleTypes") or []:
            sub = _selection(p["name"], depth + 1)
            if sub:
                parts.append(f"... on {p['name']} {{ {sub} }}")
        return " ".join(parts)
    for f in t.get("fields") or []:
        if f.get("args") and any(a["type"]["kind"] == "NON_NULL" for a in f["args"]):
            continue
        name, kind = _unwrap(f["type"])
        if kind in ("SCALAR", "ENUM"):
            parts.append(f["name"])
        elif kind in ("OBJECT", "INTERFACE", "UNION") and depth < 3 and f["name"] not in SKIP_FIELDS:
            sub = _selection(name, depth + 1)
            if sub:
                parts.append(f"{f['name']} {{ {sub} }}")
    return " ".join(parts) if parts else None

_oracle_sel = None

def oracle_selection():
    global _oracle_sel
    if _oracle_sel is None:
        _oracle_sel = _selection("OracleData") or ""
    return _oracle_sel

# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------
def discover(curator_keys, chains, min_tvl):
    targets, all_vaults = [], {}
    for builder, key, tvl_of, ver in ((ms.q_list_v2, "vaultV2s", lambda it: it.get("totalAssetsUsd"), "V2"),
                                      (ms.q_list_v1, "vaults", lambda it: (it.get("state") or {}).get("totalAssetsUsd"), "V1")):
        for it in ms.fetch_list(builder, key, chains, min_tvl, tvl_of):
            all_vaults[it["address"].lower()] = it["name"]
            name = it["name"].lower()
            for ck in curator_keys:
                cfg = CURATORS[ck]
                if any(k in name for k in cfg["keywords"]) and not any(x in name for x in cfg.get("exclude", [])):
                    targets.append({"curator": ck, "version": ver, "address": it["address"],
                                    "chain": it["chain"]["id"], "name": it["name"], "tvl": tvl_of(it) or 0})
    return targets, all_vaults

def fetch_vault(t):
    if t["version"] == "V2":
        r = ms.gql_with_fallback(ms.q_vault_v2, {"address": t["address"], "chain": t["chain"]})
        v = ms.norm_v2(r["data"]["vaultV2ByAddress"], t["chain"])
    else:
        r = ms.gql_with_fallback(ms.q_vault_v1, {"address": t["address"], "chain": t["chain"]})
        v = ms.norm_v1(r["data"]["vaultByAddress"], t["chain"])
    RAW.setdefault("vaults", {})[t["address"]] = r
    v["price"] = (v["total_usd"] / v["total"]) if v["total_usd"] and v["total"] else 1.0
    return v

_market_cache = {}

def fetch_market(mid, chain):
    key = (chain, mid.lower())
    if key in _market_cache:
        return _market_cache[key]
    base = """marketId lltv
        collateralAsset { address symbol decimals price { usd } }
        loanAsset { address symbol decimals price { usd } }
        state { price supplyAssets borrowAssets collateralAssets liquidityAssets utilization supplyApy borrowApy }"""
    m = None
    sel = oracle_selection()
    queries = []
    if sel:
        queries.append("""{ marketById(marketId: "%s", chainId: %d) { %s oracle { address type data { %s } } } }"""
                       % (mid, chain, base, sel))
    queries.append("""{ marketById(marketId: "%s", chainId: %d) { %s oracle { address type } } }""" % (mid, chain, base))
    for q in queries:
        try:
            m = gql(q).get("marketById")
            if m:
                break
        except Exception as e:
            print(f"      market {mid[:10]} fetch failed: {str(e)[:150]}")
    RAW.setdefault("markets", {})[f"{chain}:{mid}"] = m
    _market_cache[key] = m
    return m

def fetch_borrowers(mid, first=100):
    q = """{ marketPositions(first: %d, orderBy: BorrowShares, orderDirection: Desc,
             where: { marketUniqueKey_in: ["%s"] }) {
             items { user { address } healthFactor state { collateral borrowAssets borrowAssetsUsd } } } }""" % (first, mid)
    try:
        items = (gql(q).get("marketPositions") or {}).get("items") or []
    except Exception as e:
        print(f"      borrowers {mid[:10]} fetch failed: {e}")
        items = []
    RAW.setdefault("borrowers", {})[mid] = items
    return [i for i in items if num((i.get("state") or {}).get("borrowAssets")) > 0]

def fetch_history(mid, chain, days):
    t1 = int(time.time()); t0 = t1 - days * 86400
    for interval in ("HOUR", "DAY"):
        opt = "options: { startTimestamp: %d, endTimestamp: %d, interval: %s }" % (t0, t1, interval)
        q = """{ marketById(marketId: "%s", chainId: %d) { historicalState {
              borrowAssets(%s) { x y } supplyAssets(%s) { x y } utilization(%s) { x y } borrowApy(%s) { x y } } } }""" % (
            mid, chain, opt, opt, opt, opt)
        try:
            h = ((gql(q).get("marketById") or {}).get("historicalState")) or {}
            if h.get("utilization"):
                RAW.setdefault("history", {})[mid] = {"interval": interval, "data": h}
                return h, interval
        except Exception as e:
            print(f"      history {mid[:10]} ({interval}) failed: {str(e)[:120]}")
    return None, None

def fetch_depositors(t, max_pages=5):
    """largest depositors (reads up to max_pages x 100 positions, then sorts)"""
    out = []
    for page in range(max_pages):
        skip = page * 100
        if t["version"] == "V2":
            q = """{ vaultV2ByAddress(address: "%s", chainId: %d) {
                     positions(first: 100, skip: %d) { items { user { address } assetsUsd } } } }""" % (
                t["address"], t["chain"], skip)
            items = ((gql(q).get("vaultV2ByAddress") or {}).get("positions") or {}).get("items") or []
            got = [(i["user"]["address"], num(i.get("assetsUsd"))) for i in items]
        else:
            q = """{ vaultPositions(first: 100, skip: %d, orderBy: Shares, orderDirection: Desc,
                     where: { vaultAddress_in: ["%s"], chainId_in: [%d] }) {
                     items { user { address } state { assetsUsd } } } }""" % (skip, t["address"], t["chain"])
            try:
                items = (gql(q).get("vaultPositions") or {}).get("items") or []
            except ms.ValidationError:      # older schema without chainId filter
                q = q.replace(", chainId_in: [%d]" % t["chain"], "")
                items = (gql(q).get("vaultPositions") or {}).get("items") or []
            got = [(i["user"]["address"], num((i.get("state") or {}).get("assetsUsd"))) for i in items]
        out += got
        if len(items) < 100 or t["version"] == "V1":     # V1 comes sorted by size
            break
    RAW.setdefault("depositors", {})[t["address"]] = out
    return sorted(out, key=lambda p: -p[1])

_ident_cache = {}

def identify(addr, chain, known_vaults):
    """name of the Morpho vault at this address, if it is one"""
    a = addr.lower()
    if a in known_vaults:
        return known_vaults[a]
    if (a, chain) in _ident_cache:
        return _ident_cache[(a, chain)]
    name = None
    for root in ("vaultV2ByAddress", "vaultByAddress"):
        try:
            d = gql('{ %s(address: "%s", chainId: %d) { name } }' % (root, addr, chain)).get(root)
            if d and d.get("name"):
                name = d["name"]; break
        except Exception:
            pass
    _ident_cache[(a, chain)] = name
    return name

# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------
def oracle_price(m):
    """loan tokens per 1 collateral token, from Morpho's oracle"""
    p = num((m.get("state") or {}).get("price"))
    cd, ld = int(num(m["collateralAsset"]["decimals"])), int(num(m["loanAsset"]["decimals"]))
    return p / 1e36 * 10 ** cd / 10 ** ld if p else None

def coll_usd(m, op):
    cu = num(((m["collateralAsset"].get("price") or {}).get("usd")))
    if cu:
        return cu
    lu = num(((m["loanAsset"].get("price") or {}).get("usd"))) or 1.0
    return (op or 0) * lu

def loan_usd(m):
    return num(((m["loanAsset"].get("price") or {}).get("usd"))) or 1.0

def market_label(m):
    return f"{m['collateralAsset']['symbol']}/{m['loanAsset']['symbol']} ({num(m['lltv'])/1e16:.1f}%)"

# ---------------------------------------------------------------------------
# Module 1: oracle composition + gap
# ---------------------------------------------------------------------------
ZERO = "0x0000000000000000000000000000000000000000"

def _walk_oracle(o, path, feeds, vaults):
    """collect price feeds and vaults from the oracle's data, whatever the API calls their fields"""
    if isinstance(o, list):
        for x in o:
            _walk_oracle(x, path, feeds, vaults)
        return
    if not isinstance(o, dict):
        return
    last = path.split(".")[-1].lower() if path else ""
    addr = o.get("address") or o.get("id")
    if "feed" in last:
        if addr and str(addr).lower() == ZERO:
            return
        desc = o.get("description")
        if not desc and isinstance(o.get("pair"), list):
            desc = " / ".join(str(x) for x in o["pair"])
        feeds.append((path, desc or o.get("vendor") or (short(addr) if addr else "feed")))
        return
    if "vault" in last:
        if addr and str(addr).lower() != ZERO:
            vaults.append((path, addr))
        return
    for k, v in o.items():
        _walk_oracle(v, f"{path}.{k}" if path else k, feeds, vaults)

def classify_oracle(m):
    """what the oracle prices the collateral off, and whether a collateral depeg moves it"""
    data = ((m.get("oracle") or {}).get("data"))
    if data is None:
        return {"kind": "unknown", "moves_on_depeg": None, "source": "oracle details unavailable"}
    feeds, vaults = [], []
    _walk_oracle(data, "", feeds, vaults)
    base_feeds = [d for p, d in feeds if "base" in p.lower()]
    quote_feeds = [d for p, d in feeds if "quote" in p.lower()]
    base_vault = any("base" in p.lower() or p.lower().endswith("vault") for p, _ in vaults)
    cs = base_symbol(m["collateralAsset"]["symbol"])
    src = " × ".join(base_feeds) if base_feeds else ""
    if base_vault:
        src = ("vault rate" + (" × " + src if src else ""))
    if quote_feeds:
        src = (src or "1") + " ÷ " + " × ".join(quote_feeds)
    if not feeds and not vaults:
        # nothing readable: never assume the oracle is fixed
        return {"kind": "unknown", "moves_on_depeg": None, "depeg_txt": "?",
                "source": "could not read feeds (see raw.json)"}
    if not base_feeds:
        kind, moves, txt = ("exchange rate" if base_vault else "fixed"), False, "no"
    else:
        bases = [d.split("/")[0].strip() for d in base_feeds if "/" in (d or "")]
        if cs in [b.lower() for b in bases]:
            kind, moves, txt = "collateral market price", True, "yes"
        else:
            kind, moves = "underlying price", False
            txt = "only if " + " / ".join(b for b in bases if b.lower() not in ("usd", "eth")) + " moves" \
                if bases else "only via underlying"
    return {"kind": kind, "moves_on_depeg": moves, "depeg_txt": txt, "source": src or "-"}

def module_oracle(m, chain):
    op = oracle_price(m)
    lltv = num(m["lltv"]) / 1e18
    res = {"oracle_price": op, "lltv": lltv, "buffer": 1 - lltv}
    res.update(classify_oracle(m))
    cu = coll_usd(m, op)
    if not op or not cu:
        res.update(dex_price=None, gap=None, status="no oracle/usd price", flag="amber")
        return res
    ca, la = m["collateralAsset"], m["loanAsset"]
    dp, status = sell_price(chain, ca["address"], int(num(ca["decimals"])), la["address"],
                            int(num(la["decimals"])), REF_QUOTE_USD / cu)
    res["dex_price"], res["status"] = dp, status
    if dp is None:
        res["gap"] = None
        # no on-chain route = liquidations depend on redemptions / OTC
        res["flag"] = "red" if status == "no_route" else "grey"
        return res
    gap = dp / op - 1
    res["gap"] = gap
    bonus = lif(lltv) - 1
    res["liq_unprofitable_now"] = (1 + bonus) * (1 + gap) < 1     # even a $10K liquidation loses money
    if gap <= -(1 - lltv) or res["liq_unprofitable_now"]:
        res["flag"] = "red"
    elif gap <= -0.5 * (1 - lltv) or gap <= -0.5 * bonus:
        res["flag"] = "amber"
    else:
        res["flag"] = "green"
    return res

# ---------------------------------------------------------------------------
# Module 2: liquidations
# ---------------------------------------------------------------------------
def capacity(chain, token_in, dec_in, token_out, dec_out, oracle_out_per_in, lif_, in_usd):
    """largest sale (USD) of token_in whose proceeds still beat the oracle value / LIF,
    i.e. the largest liquidation a liquidator can close at a profit in one trade"""
    if not in_usd or not oracle_out_per_in:
        return None
    best = 0.0
    for usd in CAPACITY_STEPS:
        p, status = sell_price(chain, token_in, dec_in, token_out, dec_out, usd / in_usd)
        if p is None or lif_ * p / oracle_out_per_in <= 1:
            break
        best = usd
    return best

def cliff(pos, share):
    """price drop at which `share` of the borrowed amount becomes liquidatable"""
    tot = sum(d for _, d, _ in pos)
    if not tot:
        return None
    cum = 0.0
    for hf, d, _ in sorted(pos, key=lambda p: p[0]):
        cum += d
        if cum >= share * tot:
            return max(0.0, 1 - 1 / hf) if hf > 0 else 0.0
    return None

def module_liquidation(m, chain, borrowers, vault_share, oracle_info):
    lltv = num(m["lltv"]) / 1e18
    L = lif(lltv)
    ca, la = m["collateralAsset"], m["loanAsset"]
    cd, ld = int(num(ca["decimals"])), int(num(la["decimals"]))
    lu = loan_usd(m)
    op = oracle_price(m)
    cu = coll_usd(m, op)
    total_borrow = num(m["state"]["borrowAssets"]) / 10 ** ld
    correlated = family(ca["symbol"]) is not None and family(ca["symbol"]) == family(la["symbol"])
    shocks = [0.02, 0.05, 0.10] if correlated else [0.10, 0.20, 0.30]
    pos = []
    for b in borrowers:
        st = b.get("state") or {}
        hf = b.get("healthFactor")
        debt = num(st.get("borrowAssets")) / 10 ** ld
        coll = num(st.get("collateral")) / 10 ** cd
        if hf is None and op and debt:
            hf = coll * op * lltv / debt
        if hf is None or debt <= 0:
            continue
        pos.append((num(hf), debt, coll, b["user"]["address"]))
    p3 = [(h, d, c) for h, d, c, _ in pos]
    covered = sum(p[1] for p in p3)
    top = max(pos, key=lambda p: p[1]) if pos else None
    res = {"lltv": lltv, "bonus": L - 1, "correlated": correlated, "shocks": [],
           "borrow_total_usd": total_borrow * lu,
           "borrow_covered_pct": covered / total_borrow if total_borrow else None,
           "n_positions": len(pos), "min_hf": min((p[0] for p in pos), default=None),
           "top_borrower": top[3] if top else None,
           "top_borrower_share": top[1] / total_borrow if top and total_borrow else None,
           "cliff25": cliff(p3, 0.25), "cliff50": cliff(p3, 0.50),
           "oracle_moves_on_depeg": (oracle_info or {}).get("moves_on_depeg"),
           "oracle_kind": (oracle_info or {}).get("kind")}
    # capacity: into the loan token (atomic on-chain liquidator) and into USDC (loan-token leg removed)
    res["capacity_loan"] = capacity(chain, ca["address"], cd, la["address"], ld, op, L, cu) if op else None
    usdc = USDC.get(chain)
    if usdc and la["address"].lower() != usdc.lower() and op:
        res["capacity_usdc"] = capacity(chain, ca["address"], cd, usdc, 6, op * lu, L, cu)
    else:
        res["capacity_usdc"] = res["capacity_loan"]
    for s in shocks:
        liq = [(hf, debt, coll) for hf, debt, coll in p3 if hf * (1 - s) < 1]
        sell_usd = sum(c for _, _, c in liq) * (op or 0) * lu * (1 - s)
        t_loan = (None if res["capacity_loan"] is None or not sell_usd else
                  (sell_usd / res["capacity_loan"] if res["capacity_loan"] > 0 else float("inf")))
        t_usdc = (None if res["capacity_usdc"] is None or not sell_usd else
                  (sell_usd / res["capacity_usdc"] if res["capacity_usdc"] > 0 else float("inf")))
        # positions already underwater after the shock: bad debt even with perfect liquidations
        underwater = sum(max(0.0, debt - coll * (op or 0) * (1 - s) / L) for _, debt, coll in liq)
        res["shocks"].append({"shock": s, "n_liquidatable": len(liq),
                              "debt_liquidatable_usd": sum(d for _, d, _ in liq) * lu,
                              "collateral_to_sell_usd": sell_usd,
                              "trades_loan": t_loan, "trades_usdc": t_usdc,
                              "bad_debt_perfect_usd": underwater * lu,
                              "vault_bad_debt_usd": underwater * lu * vault_share})
    mid = res["shocks"][1]
    tl, tu = mid["trades_loan"] or 0, mid["trades_usdc"] or 0
    # flags come from liquidity only; the oracle kind is reported next to them, never used to hide a flag
    if not mid["collateral_to_sell_usd"]:
        res["flag"] = "green"
    elif tl > 10 and tu > 10:
        res["flag"] = "red"           # needs >10 back-to-back max-size liquidations on any route
    elif tl > 10:
        res["flag"] = "amber"         # fine via USDC; the loan token's own DEX liquidity is the bottleneck
    elif tl > 1:
        res["flag"] = "amber"
    else:
        res["flag"] = "green"
    return res

# ---------------------------------------------------------------------------
# Module 3: borrower reaction to rates
# ---------------------------------------------------------------------------
def ols(xs, ys):
    n = len(xs)
    if n < 30:
        return None, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    ss_res = sum((y - (my + b * (x - mx))) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return b, (1 - ss_res / ss_tot) if ss_tot else None

def module_borrower(hist, interval):
    def series(name):
        return {int(p["x"]): num(p["y"]) for p in (hist.get(name) or []) if p.get("y") is not None}
    B, S, U, R = series("borrowAssets"), series("supplyAssets"), series("utilization"), series("borrowApy")
    ts = sorted(set(B) & set(U) & set(R))
    if ts:
        # skip the launch period of new markets (tiny balances make % changes meaningless)
        medB = statistics.median([B[t] for t in ts])
        start = next((i for i, t in enumerate(ts) if B[t] >= 0.2 * medB), len(ts))
        ts = ts[start:]
    if (len(ts) < 48) if interval == "HOUR" else (len(ts) < 10):
        return {"flag": "grey", "note": "not enough history"}
    step = 3600 if interval == "HOUR" else 86400
    fwd = 24 if interval == "HOUR" else 1
    med = statistics.median([R[t] for t in ts])
    xs, ys = [], []
    for i, t in enumerate(ts[:-fwd]):
        t2 = ts[i + fwd]
        if t2 - t != fwd * step or B[t] <= 0:
            continue
        xs.append((R[t] - med) * 100)
        ys.append((B[t2] - B[t]) / B[t] * 100)
    slope, r2 = ols(xs, ys)
    episodes, cur = [], None
    for t in ts:
        u = U[t]
        if cur is None and u > 0.92:
            cur = (t, B[t], S.get(t))
        elif cur is not None and u < 0.905:
            dB = (B[t] - cur[1]) / cur[1] if cur[1] else 0.0
            dS = (S[t] - cur[2]) / cur[2] if cur[2] and S.get(t) is not None else None
            episodes.append(((t - cur[0]) / 3600, dB, dS))
            cur = None
    res = {"points": len(ts), "interval": interval, "median_borrow_apy": med,
           "sensitivity_pct_per_pp": slope if (r2 or 0) >= 0.1 else None, "r2": r2,
           "spikes": len(episodes),
           "median_spike_hours": statistics.median([e[0] for e in episodes]) if episodes else None,
           "spike_borrow_change": statistics.median([e[1] for e in episodes]) if episodes else None,
           "spike_supply_change": (statistics.median([e[2] for e in episodes if e[2] is not None])
                                   if any(e[2] is not None for e in episodes) else None),
           "current_spike_hours": (ts[-1] - cur[0]) / 3600 if cur else 0,
           "time_above_92": sum(1 for t in ts if U[t] > 0.92) / len(ts),
           "util_now": U[ts[-1]], "apy_now": R[ts[-1]]}
    if res["current_spike_hours"] > 72:
        res["flag"] = "red"           # stuck above the kink for 3+ days
    elif res["time_above_92"] > 0.2:
        res["flag"] = "amber"         # above the kink a fifth of the time
    else:
        res["flag"] = "green"
    return res

# ---------------------------------------------------------------------------
# Module 4: runs
# ---------------------------------------------------------------------------
def withdrawable_usd(v):
    tot = v.get("idle_usd") or 0.0
    for m in v["markets"]:
        if m["S"] > 0:
            tot += max(0.0, min(m["x"], m["S"] - m["B"])) * v["price"]
    return tot

def drained_borrow_apr(v):
    """borrow APR the curve charges once the vault's markets hit 100% utilization:
    4x the rate at target, which then doubles roughly every 5 days while utilization stays at 100%"""
    w = [(m["x"], m.get("rat_apr")) for m in v["markets"] if m.get("rat_apr") and m["x"] > 0]
    tot = sum(x for x, _ in w)
    return 4 * sum(x * r for x, r in w) / tot if tot else None

def module_runs(v, deps):
    tvl = v["total_usd"] or 0
    w = withdrawable_usd(v)
    amounts = [a for _, a in deps]
    top = lambda n: sum(amounts[:n])
    gap = max(0.0, top(1) - w)
    res = {"tvl": tvl, "withdrawable": w, "withdrawable_pct": w / tvl if tvl else None,
           "n_depositors_read": len(deps),
           "top1": top(1), "top5": top(5), "top10": top(10),
           "top1_pct": top(1) / tvl if tvl else None, "top5_pct": top(5) / tvl if tvl else None,
           "exit_gap": gap, "exit_gap_pct": gap / tvl if tvl else None,
           "drained_apr": drained_borrow_apr(v),
           "largest": [[a, x, None] for a, x in deps[:5]]}
    if res["top1_pct"] and res["top1_pct"] >= 0.9:
        res["flag"], res["dedicated"] = "info", True     # one-client vault: not a run, a mandate
    elif res["exit_gap_pct"] and res["exit_gap_pct"] > 0.15:
        res["flag"] = "red"
    elif gap > 0:
        res["flag"] = "amber"
    else:
        res["flag"] = "green"
    return res

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
FLAG = {"red": "RED", "amber": "AMBER", "green": "OK", "grey": "n/a", "info": "INFO"}

def by_exposure(market_results):
    return sorted(market_results.items(), key=lambda kv: -kv[1]["exposure"])

def findings(vaults, market_results, material):
    out = []          # (severity, exposure, text)
    for v in vaults:
        r = v.get("runs")
        if not r:
            continue
        if r["flag"] in ("red", "amber"):
            who = r["largest"][0]
            tag = f" — {who[2]}" if who[2] else ""
            out.append((r["flag"], v["total_usd"] or 0,
                        f"**Liquidity — {v['name']}**: largest depositor ({short(who[0])}{tag}) holds "
                        f"{fmt_usd(r['top1'])} ({pct(r['top1_pct'],0)} of TVL); {fmt_usd(r['withdrawable'])} "
                        f"is withdrawable now, so a full exit leaves {fmt_usd(r['exit_gap'])} waiting on borrowers. "
                        f"Rate curve would then charge ~{pct(r['drained_apr'],0)} APR, doubling about every 5 days."))
    for key, mr in by_exposure(market_results):
        if mr["exposure"] < material:
            continue
        o, l, b = mr.get("oracle") or {}, mr.get("liq") or {}, mr.get("borrower") or {}
        if o.get("flag") == "red":
            if o.get("gap") is None:
                txt = "no on-chain DEX route for the collateral; liquidations depend on redemption or OTC"
            elif o.get("liq_unprofitable_now"):
                txt = (f"collateral sells {pct(-o['gap'],1)} below the oracle even at $10K, more than the "
                       f"{pct(l.get('bonus') or lif(o['lltv'])-1,1)} liquidation bonus — liquidating today loses money")
            else:
                txt = f"oracle-vs-DEX gap {pct(o['gap'],2)} exceeds the {pct(o['buffer'])} liquidation buffer"
            out.append(("red", mr["exposure"], f"**Oracle — {mr['label']}**: {txt}"))
        if l.get("flag") in ("red", "amber"):
            s = l["shocks"][1]
            if not s["collateral_to_sell_usd"]:
                continue
            bottleneck = ""
            if (s["trades_loan"] or 0) > 10 and (s["trades_usdc"] or 0) <= 10:
                bottleneck = (f" Into USDC the same sale needs only {trades_txt(s['trades_usdc'])} trades, so the "
                              f"bottleneck is {mr['label'].split('/')[1].split(' ')[0]} DEX liquidity, not the collateral.")
            if l["capacity_loan"]:
                absorb = (f"One profitable DEX trade absorbs {fmt_usd(l['capacity_loan'])}, so clearing it takes "
                          f"~{trades_txt(s['trades_loan'])} back-to-back liquidations.")
            else:
                absorb = "No DEX sale of $100K or more clears at a profit, so on-chain liquidators would not act."
            if o.get("kind") in ("exchange rate", "fixed"):
                bottleneck += (" Note: the oracle reads an exchange rate, so a market move alone may not trigger "
                               "these liquidations; the risk is a feed or NAV change.")
            out.append((l["flag"], mr["exposure"],
                        f"**Liquidations — {mr['label']}**: a −{s['shock']*100:.0f}% move makes "
                        f"{fmt_usd(s['debt_liquidatable_usd'])} of debt liquidatable (top borrower "
                        f"{pct(l['top_borrower_share'],0)} of the market). {absorb}{bottleneck}"))
        if b.get("flag") == "red":
            out.append(("amber", mr["exposure"],
                        f"**Rates — {mr['label']}**: utilization has been above 92% for "
                        f"{b['current_spike_hours']:.0f}h at {pct(b['apy_now'],1)} borrow APY"))
    order = {"red": 0, "amber": 1}
    return sorted(out, key=lambda x: (order.get(x[0], 2), -x[1]))

def write_report(ck, vaults, market_results, cross, outdir, args, material):
    label = CURATORS[ck]["label"]
    tvl = sum(v["total_usd"] or 0 for v in vaults)
    L = [f"# {label} — Morpho risk report", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · {len(vaults)} vaults · {fmt_usd(tvl)} TVL · "
         f"{len(market_results)} markets · modules: {', '.join(args.modules)}", "",
         f"Headline findings only cover markets with at least {fmt_usd(material)} of exposure. "
         "Everything below is a snapshot: rerun before quoting a number.", ""]
    F = findings(vaults, market_results, material)
    L += ["## Key findings", ""] + ([f"- {FLAG[s]} · {t}" for s, _, t in F] if F else ["- none"]) + [""]
    if cross:
        L += ["### Linked wallets", ""] + [f"- {c}" for c in cross] + [""]

    if "runs" in args.modules:
        L += ["## Liquidity and depositors", "",
              "Exit gap = what the largest depositor could not withdraw today (their balance minus instant liquidity). "
              "RED above 15% of TVL. A vault with one depositor holding ≥90% is marked INFO: that is a single-client "
              "mandate, not a run risk. 'Borrow APR once drained' = 4 × rate at target, the rate borrowers pay the "
              "moment the vault's markets hit 100% utilization.", "",
              "| Vault | Chain | TVL | Withdrawable now | Largest depositor | Top 5 | Exit gap | Borrow APR once drained | Flag |",
              "|---|---|---|---|---|---|---|---|---|"]
        for v in sorted(vaults, key=lambda v: -(v["total_usd"] or 0)):
            r = v.get("runs") or {}
            L.append(f"| {v['name']} | {v['chain']} | {fmt_usd(v['total_usd'])} | "
                     f"{fmt_usd(r.get('withdrawable'))} ({pct(r.get('withdrawable_pct'),0)}) | "
                     f"{pct(r.get('top1_pct'),0)} | {pct(r.get('top5_pct'),0)} | "
                     f"{fmt_usd(r.get('exit_gap'))} ({pct(r.get('exit_gap_pct'),0)}) | {pct(r.get('drained_apr'),0)} | "
                     f"{FLAG.get(r.get('flag'), '-')} |")
        L += ["", "### Largest depositors", ""]
        for v in sorted(vaults, key=lambda v: -(v["total_usd"] or 0)):
            r = v.get("runs")
            if r:
                L.append(f"- **{v['name']}**: " + "; ".join(
                    f"{short(a)} {fmt_usd(x)}" + (f" ({nm})" if nm else "") for a, x, nm in r["largest"]))
        L.append("")

    if "oracle" in args.modules:
        L += ["## Oracles", "",
              "'Prices collateral via' reads the oracle's feeds. If the oracle uses an exchange rate or an underlying "
              f"asset's price, a depeg of the collateral itself does not trigger liquidations. DEX price = KyberSwap quote "
              f"for {fmt_usd(REF_QUOTE_USD)} of collateral. RED when the gap exceeds the liquidation buffer (1 − LLTV), "
              "when even a small liquidation would lose money, or when there is no route.", "",
              "| Market | Exposure | Prices collateral via | Kind | Collateral's own DEX price moves oracle? | Oracle price | DEX price | Gap | Buffer | Flag |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for key, mr in by_exposure(market_results):
            o = mr.get("oracle")
            if not o:
                continue
            mv = o.get("depeg_txt") or "?"
            L.append(f"| {mr['label']} | {fmt_usd(mr['exposure'])} | {o.get('source')} | {o.get('kind')} | {mv} | "
                     f"{fx(o.get('oracle_price'), '.6g')} | {fx(o.get('dex_price'), '.6g')} | "
                     f"{pct(o.get('gap'), 2) if o.get('gap') is not None else o.get('status')} | "
                     f"{pct(o['buffer'])} | {FLAG[o['flag']]} |")
        L.append("")

    if "liquidation" in args.modules:
        L += ["## Liquidations", "",
              "Cliff = price drop (collateral vs. loan token) at which 25% / 50% of the market's debt becomes "
              "liquidatable, from each borrower's health factor. Capacity = largest single DEX sale a liquidator can "
              "make and still profit after the liquidation bonus; measured into the loan token and into USDC. "
              "Trades = collateral to sell ÷ capacity: liquidations needed back-to-back while prices are falling "
              "(RED above 10 on both routes). Check the oracle table: where the oracle reads only an exchange rate, a "
              "market move alone does not trigger these liquidations.", "",
              "| Market | Exposure | Top borrower | Min HF | Cliff 25% | Cliff 50% | Capacity → loan | Capacity → USDC | "
              "Shock | Liquidatable debt | Collateral to sell | Trades (loan) | Trades (USDC) | Flag |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for key, mr in by_exposure(market_results):
            l = mr.get("liq")
            if not l:
                continue
            for i, s in enumerate(l["shocks"]):
                f0 = i == 0
                L.append("| " + " | ".join([
                    mr["label"] if f0 else "", fmt_usd(mr["exposure"]) if f0 else "",
                    pct(l["top_borrower_share"], 0) if f0 else "", fx(l["min_hf"], ".2f") if f0 else "",
                    ("−" + pct(l["cliff25"], 0)) if f0 and l["cliff25"] is not None else ("-" if f0 else ""),
                    ("−" + pct(l["cliff50"], 0)) if f0 and l["cliff50"] is not None else ("-" if f0 else ""),
                    fmt_usd(l["capacity_loan"]) if f0 else "", fmt_usd(l["capacity_usdc"]) if f0 else "",
                    f"−{s['shock']*100:.0f}%", fmt_usd(s["debt_liquidatable_usd"]),
                    fmt_usd(s["collateral_to_sell_usd"]), trades_txt(s["trades_loan"]), trades_txt(s["trades_usdc"]),
                    FLAG[l["flag"]] if f0 else ""]) + " |")
        L.append("")

    if "borrower" in args.modules:
        L += ["## Borrowers and rates", "",
              "Spikes = episodes above 92% utilization, measured until utilization is back under 90.5%. 'Borrow' and "
              "'supply' change show how spikes ended: borrowers repaying, or new supply (often the curator reallocating). "
              "RED when a market has sat above 92% for more than 3 days; AMBER when it spends over a fifth of the time "
              "there.", "",
              "| Market | Exposure | Util now | Borrow APY now | Time >92% | Spikes | Median spike | Borrow change | "
              "Supply change | Flag |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for key, mr in by_exposure(market_results):
            b = mr.get("borrower")
            if not b or b.get("note"):
                continue
            L.append(f"| {mr['label']} | {fmt_usd(mr['exposure'])} | {pct(b['util_now'])} | {pct(b['apy_now'],2)} | "
                     f"{pct(b['time_above_92'])} | {b['spikes']} | {fx(b['median_spike_hours'], '.0f', 'h')} | "
                     f"{pct(b['spike_borrow_change'])} | {pct(b['spike_supply_change'])} | {FLAG[b['flag']]} |")
        L.append("")
    L += ["## Method notes", "",
          "- DEX prices are KyberSwap route quotes at run time. Liquidators can also redeem with the issuer or trade "
          "OTC; capacity here is on-chain only.",
          "- Liquidation shocks: −10/20/30% for unrelated assets, −2/5/10% for same-family pairs (inferred from symbols).",
          "- Borrowers are assumed not to add collateral or repay as prices fall; the 100 largest positions are read.",
          "- Liquidity uses min(vault position, unborrowed cash) per market plus idle cash, at this moment.",
          "- A regression of borrowing on rates is kept in markets.csv (rate_sensitivity, rate_r2) but not shown: on "
          "hourly data it mostly picks up borrowing driving the rate, not the rate deterring borrowers."]
    md = "\n".join(L)
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    page = os.path.join(outdir, "report.html")
    html = ms.md_to_html(md, f"{label} risk report").replace("max-width:1200px", "max-width:1700px")
    for word, col in (("RED", "#c0392b"), ("AMBER", "#b9770e"), ("OK", "#1e8449"), ("INFO", "#2f5d8a")):
        html = html.replace(f"<td>{word}</td>", f'<td><b style="color:{col}">{word}</b></td>')
        html = html.replace(f"<li>{word} · ", f'<li><b style="color:{col}">{word}</b> · ')
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    return page

def write_csvs(vaults, market_results, outdir):
    with open(os.path.join(outdir, "vaults.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["vault", "address", "chain", "tvl_usd", "withdrawable_usd", "top1_usd", "top5_usd", "top10_usd",
                    "exit_gap_usd", "drained_borrow_apr", "flag"])
        for v in vaults:
            r = v.get("runs") or {}
            w.writerow([v["name"], v["address"], v["chain"], round(v["total_usd"] or 0), round(r.get("withdrawable") or 0),
                        round(r.get("top1") or 0), round(r.get("top5") or 0), round(r.get("top10") or 0),
                        round(r.get("exit_gap") or 0), r.get("drained_apr"), r.get("flag")])
    with open(os.path.join(outdir, "depositors.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["vault", "rank", "depositor", "usd", "label"])
        for v in vaults:
            for i, (a, x, nm) in enumerate((v.get("runs") or {}).get("largest") or [], 1):
                w.writerow([v["name"], i, a, round(x), nm or ""])
    cols = ["market", "market_id", "chain", "vault_exposure_usd", "lltv",
            "oracle_kind", "oracle_source", "depeg_triggers_liquidation", "oracle_price", "dex_price", "oracle_gap",
            "oracle_flag", "liq_bonus", "min_hf", "top_borrower", "top_borrower_share", "cliff25", "cliff50",
            "capacity_loan_usd", "capacity_usdc_usd"]
    for k in (1, 2, 3):
        cols += [f"shock{k}", f"shock{k}_liquidatable_debt_usd", f"shock{k}_collateral_to_sell_usd",
                 f"shock{k}_trades_loan", f"shock{k}_trades_usdc", f"shock{k}_bad_debt_perfect_usd"]
    cols += ["liq_flag", "util_now", "time_above_92", "spikes", "median_spike_hours", "spike_borrow_change",
             "spike_supply_change", "rate_sensitivity", "rate_r2", "borrower_flag"]
    with open(os.path.join(outdir, "markets.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for key, mr in by_exposure(market_results):
            o, l, b = mr.get("oracle") or {}, mr.get("liq") or {}, mr.get("borrower") or {}
            row = [mr["label"], key[1], key[0], round(mr["exposure"]), mr.get("lltv"),
                   o.get("kind"), o.get("source"), o.get("moves_on_depeg"), o.get("oracle_price"), o.get("dex_price"),
                   o.get("gap"), o.get("flag"), l.get("bonus"), l.get("min_hf"), l.get("top_borrower"),
                   l.get("top_borrower_share"), l.get("cliff25"), l.get("cliff50"), l.get("capacity_loan"),
                   l.get("capacity_usdc")]
            for s in (l.get("shocks") or [{}, {}, {}]):
                row += [s.get("shock"), s.get("debt_liquidatable_usd"), s.get("collateral_to_sell_usd"),
                        s.get("trades_loan"), s.get("trades_usdc"), s.get("bad_debt_perfect_usd")]
            row += [l.get("flag"), b.get("util_now"), b.get("time_above_92"), b.get("spikes"),
                    b.get("median_spike_hours"), b.get("spike_borrow_change"), b.get("spike_supply_change"),
                    b.get("sensitivity_pct_per_pp"), b.get("r2"), b.get("flag")]
            w.writerow(row)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def link_wallets(vaults, market_results, borrower_book):
    """depositors that appear in several of the curator's vaults, or that also borrow from its markets"""
    seen = {}
    for v in vaults:
        for a, x, _ in (v.get("runs") or {}).get("largest") or []:
            seen.setdefault(a.lower(), []).append((v["name"], x))
    notes = []
    for a, lst in sorted(seen.items(), key=lambda kv: -sum(x for _, x in kv[1])):
        borrows = borrower_book.get(a, [])
        if len(lst) < 2 and not borrows:
            continue
        txt = f"{short(a)} deposits " + ", ".join(f"{fmt_usd(x)} in {n}" for n, x in lst)
        if borrows:
            txt += "; also borrows " + ", ".join(f"{fmt_usd(d)} from {lab}" for lab, d in borrows)
        notes.append(txt)
        for v in vaults:
            for item in (v.get("runs") or {}).get("largest") or []:
                if item[0].lower() == a:
                    extra = []
                    if len(lst) > 1:
                        extra.append(f"in {len(lst)} vaults")
                    if borrows:
                        extra.append("also a borrower")
                    item[2] = "; ".join(x for x in [item[2]] + extra if x)
    return notes

def run_curator(ck, targets, known_vaults, args):
    label = CURATORS[ck]["label"]
    mine = [t for t in targets if t["curator"] == ck]
    if args.max_vaults:
        mine = sorted(mine, key=lambda t: -t["tvl"])[:args.max_vaults]
    print(f"\n=== {label}: {len(mine)} vaults ===")
    if not mine:
        print("   no vaults matched - check CURATORS keywords, --chains or --min-tvl")
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    outdir = os.path.join("risk_out", f"{ck}_{stamp}")
    os.makedirs(outdir, exist_ok=True)
    vaults, market_results = [], {}
    for i, t in enumerate(mine, 1):
        print(f"[{i}/{len(mine)}] {t['name']} ({fmt_usd(t['tvl'])})")
        try:
            v = fetch_vault(t)
        except Exception as e:
            print(f"   skipped: {e}"); continue
        v.update(name=t["name"], address=t["address"], chain=t["chain"], version=t["version"])
        if "runs" in args.modules:
            try:
                v["runs"] = module_runs(v, fetch_depositors(t))
                for item in v["runs"]["largest"]:
                    item[2] = identify(item[0], t["chain"], known_vaults)
            except Exception as e:
                print(f"   run coverage failed: {e}")
        vaults.append(v)
        for mk in v["markets"]:
            exposure = mk["x"] * v["price"]
            if exposure < MIN_EXPOSURE_USD or mk["S"] <= 0:
                continue
            key = (t["chain"], mk["market_id"].lower())
            mr = market_results.get(key)
            if mr:                                   # same market seen in another vault of this curator
                mr["exposure"] += exposure
                mr["vault_share"] += mk["x"] / mk["S"]
                continue
            m = fetch_market(mk["market_id"], t["chain"])
            if not m or not m.get("collateralAsset"):
                continue
            market_results[key] = {"label": market_label(m), "exposure": exposure,
                                   "vault_share": mk["x"] / mk["S"], "lltv": num(m["lltv"]) / 1e18, "m": m}
    tvl = sum(v["total_usd"] or 0 for v in vaults)
    material = max(MATERIAL_USD, 0.005 * tvl)
    borrower_book = {}
    print(f"   analysing {len(market_results)} markets...")
    for j, (key, mr) in enumerate(by_exposure(market_results), 1):
        chain, mid = key; m = mr["m"]
        print(f"   ({j}/{len(market_results)}) {mr['label']}")
        try:
            if "oracle" in args.modules or "liquidation" in args.modules:
                mr["oracle"] = module_oracle(m, chain)
            if "liquidation" in args.modules:
                bs = fetch_borrowers(mid, args.borrowers)
                ld = int(num(m["loanAsset"]["decimals"]))
                for b in bs:
                    borrower_book.setdefault(b["user"]["address"].lower(), []).append(
                        (mr["label"], num(b["state"]["borrowAssets"]) / 10 ** ld * loan_usd(m)))
                mr["liq"] = module_liquidation(m, chain, bs, min(1.0, mr["vault_share"]), mr.get("oracle"))
            if "borrower" in args.modules:
                h, interval = fetch_history(mid, chain, args.history_days)
                mr["borrower"] = module_borrower(h, interval) if h else {"flag": "grey", "note": "no history"}
        except Exception as e:
            print(f"      failed: {type(e).__name__}: {e}")
    if "oracle" not in args.modules:
        for mr in market_results.values():
            mr.pop("oracle", None)
    for mr in market_results.values():
        mr.pop("m", None)
    for lst in borrower_book.values():           # keep only material borrows for the wallet notes
        lst[:] = [x for x in lst if x[1] >= 1e6]
    cross = link_wallets(vaults, market_results, borrower_book)
    page = write_report(ck, vaults, market_results, cross, outdir, args, material)
    write_csvs(vaults, market_results, outdir)
    with open(os.path.join(outdir, "raw.json"), "w", encoding="utf-8") as f:
        json.dump(RAW, f)
    print(f"   -> {page}")
    return page

def main():
    ap = argparse.ArgumentParser(description="Morpho curator risk engine")
    ap.add_argument("--curator", default="sentora", help="curator key, or 'all' (see --list-curators)")
    ap.add_argument("--modules", nargs="+", default=["runs", "oracle", "liquidation", "borrower"],
                    choices=["runs", "oracle", "liquidation", "borrower"])
    ap.add_argument("--chains", type=int, nargs="+", default=[1, 8453, 42161, 130, 137])
    ap.add_argument("--min-tvl", type=float, default=1_000_000)
    ap.add_argument("--max-vaults", type=int, help="only the N largest vaults per curator (quick test)")
    ap.add_argument("--borrowers", type=int, default=100, help="largest borrowers read per market")
    ap.add_argument("--history-days", type=int, default=30)
    ap.add_argument("--list-curators", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    if args.list_curators:
        for k, c in CURATORS.items():
            print(f"{k:12} {c['label']:12} matches vault names containing: {', '.join(c['keywords'])}")
        return
    keys = list(CURATORS) if args.curator == "all" else [args.curator.lower()]
    for k in keys:
        if k not in CURATORS:
            sys.exit(f"unknown curator '{k}'. Use --list-curators")
    print("Finding vaults...")
    targets, known = discover(keys, args.chains, args.min_tvl)
    pages = [p for p in (run_curator(k, targets, known, args) for k in keys) if p]
    if pages and not args.no_open:
        try:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(pages[0]))
        except Exception:
            pass
    print("\nDone.")

if __name__ == "__main__":
    main()
