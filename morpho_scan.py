#!/usr/bin/env python3
"""
Morpho allocation-efficiency scanner
====================================

Scores every Morpho vault (V1 MetaMorpho and V2) above a TVL cutoff on how
efficiently its money is spread across lending markets.

For every market a vault lends into it computes:
  * utilization, borrow rate and supply APY, rebuilt from Morpho's
    AdaptiveCurveIRM (checked against the API's own APY)
  * the vault's share of the market
  * marginal return: how much the vault's yearly income changes if it adds
    one more $10k (accounts for the extra money lowering the rate on the
    vault's existing balance)
  * how fast the market's base rate is drifting (30-day factor if
    utilization stays where it is)

And four vault-level signals:
  1. ALLOCATION GAP  - $/yr gained by moving money from low-marginal to
                       high-marginal markets (conservative: never pushes a
                       market above the 90% kink by withdrawing from it)
  2. IDLE CASH       - money earning 0%, and what redeploying it (above a
                       buffer) would earn
  3. DOMINANT MARKETS - markets where the vault is >50% of supply; there the
                       question is pricing (utilization), not allocation
  4. DEAD MARKETS    - matured PTs / near-empty markets still in the vault

Usage (Python 3.8+, no installs needed):
    python3 morpho_scan.py                       # Ethereum + Base, TVL >= $20M
    python3 morpho_scan.py --min-tvl 50000000 --chains 1 8453 42161
    python3 morpho_scan.py --offline-v2 saved.json   # analyse one saved V2 query result

Output: a timestamped folder under ./morpho_scan_out/ containing
    vaults.csv    one row per vault, ranked by allocation gap ($/yr)
    markets.csv   one row per vault x market, with every intermediate number
    summary.md / summary.html   top vaults in plain English (HTML opens automatically)
    raw/          raw API responses (keep these - they are your history)

Also used as the shared Morpho API / rate-model library by risk_engine.py and
liquidation_engine.py.

Limitations (also printed in summary.md):
  * V2 market and collateral caps are applied; V1 supply caps are applied.
    Incentive rewards are included as a fixed budget shared pro-rata.
  * Borrowing is held fixed when money moves. Fine for small moves over
    hours/days, not for large moves over weeks.
  * One snapshot can be noise. Run daily and only trust gaps that persist.
"""

import argparse, csv, html, json, math, os, re, sys, time, urllib.request, urllib.error, webbrowser
from datetime import datetime, timezone

API = "https://api.morpho.org/graphql"
YEAR = 365 * 24 * 3600
BUMP_USD = 10_000          # size of the "one more dollar" test
TARGET_U = 0.90            # Morpho kink
SLEEP = 0.4                # seconds between API calls

# ---------------------------------------------------------------------------
# Morpho AdaptiveCurveIRM
# ---------------------------------------------------------------------------

def rat_to_apr(rat_raw):
    """rateAtTarget from the API is per-second, scaled by 1e18."""
    return float(rat_raw) / 1e18 * YEAR

def curve_factor(u):
    if u <= TARGET_U:
        return 0.25 + 0.75 * u / TARGET_U          # 0.25 at u=0 -> 1 at 90%
    return 1 + 3 * (u - TARGET_U) / (1 - TARGET_U)  # 1 at 90% -> 4 at 100%

def borrow_apr(u, rat_apr):
    return rat_apr * curve_factor(min(u, 1.0))

def supply_apy(S, B, rat_apr, fee=0.0):
    if S <= 0:
        return 0.0
    u = min(B / S, 1.0)
    return (math.exp(borrow_apr(u, rat_apr)) - 1) * u * (1 - fee)

def drift_30d(u):
    """Factor by which rateAtTarget changes in 30 days if utilization stays put."""
    err = (u - TARGET_U) / TARGET_U if u <= TARGET_U else (u - TARGET_U) / (1 - TARGET_U)
    return math.exp(50 * err * 30 / 365)

REWARDS = True   # include incentive rewards (toggled off once to report the native-only gap)

def income(m, x_new, group=True):
    """Yearly income from market m if this vault's balance there were x_new (tokens).
    group=True counts the curator's other vaults in the same market too (x_sib),
    so moving money into a market a sibling vault already dominates is not
    mistaken for a gain."""
    S_new = m["S"] - m["x"] + x_new
    held = max(x_new, 0.0) + (m.get("x_sib", 0.0) if group else 0.0)
    if S_new <= 0 or held <= 0:
        return 0.0
    native = held * supply_apy(S_new, m["B"], m["rat_apr"])
    # rewards: treated as a fixed yearly budget (reward APR x current supply)
    # shared pro-rata by all suppliers, so adding supply dilutes the APR
    reward = held / S_new * m.get("reward_apr", 0.0) * m["S"] if REWARDS else 0.0
    return native + reward

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass

def gql(query, variables=None, tries=4):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(API, data=body, headers={
            "content-type": "application/json", "user-agent": "morpho-scan/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="ignore")
            if "GRAPHQL_VALIDATION_FAILED" in txt or "Cannot query field" in txt:
                raise ValidationError(txt[:500])
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt * 2); continue
            raise RuntimeError(f"HTTP {e.code}: {txt[:300]}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt * 2); continue
            raise
        if out.get("errors"):
            msg = json.dumps(out["errors"])[:500]
            if "GRAPHQL_VALIDATION_FAILED" in msg or "Cannot query field" in msg:
                raise ValidationError(msg)
            if out.get("data"):
                return out          # partial data (e.g. one vault not found)
            raise RuntimeError(msg)
        time.sleep(SLEEP)
        return out
    raise RuntimeError("gave up after retries")

# Optional fields are dropped automatically if the API schema doesn't have them.
OPTIONAL = {
    "v1_cap": "supplyCap",
    "v2_pageinfo": "pageInfo { countTotal }",
    "mkt_rewards": "rewards { supplyApr }",
    "v2_owner": "owner { address }",
    "v2_curators": "curators { items { addresses { address } } }",
    "v1_roles": "curator owner",
    "v2_caps": """caps(first: 100) { items { type absoluteCap relativeCap allocation
      data { __typename
        ... on CollateralCapData { collateralAddress }
        ... on MarketV1CapData { marketParams { id } }
        ... on AdapterCapData { adapterAddress } } } }""",
}
DISABLED = set()

def opt(key):
    return "" if key in DISABLED else OPTIONAL[key]

def gql_with_fallback(build, variables=None):
    """build() returns a query string using opt(); drop optional fields on schema errors."""
    while True:
        try:
            return gql(build(), variables)
        except ValidationError as e:
            msg = str(e).replace("\\", "")
            dropped = False
            for key, frag in OPTIONAL.items():
                if key in DISABLED:
                    continue
                first_field = re.findall(r"[A-Za-z]+", frag)[0]
                if f'"{first_field}"' in msg:
                    DISABLED.add(key); dropped = True
                    print(f"  (API has no field '{first_field}', continuing without it)")
                    break
            if not dropped:
                raise

def mf():
    return """
  marketId
  collateralAsset { symbol address }
  loanAsset { symbol decimals }
  lltv
  state { supplyAssets borrowAssets utilization supplyApy rateAtTarget %s }
""" % opt("mkt_rewards")

def q_list_v2():
    return """query($chains:[Int!], $skip:Int!) {
  vaultV2s(first: 100, skip: $skip, where: { chainId_in: $chains },
           orderBy: TotalAssetsUsd, orderDirection: Desc) {
    items { address name chain { id } asset { symbol decimals } totalAssets totalAssetsUsd }
  }
}"""

def q_list_v1():
    return """query($chains:[Int!], $skip:Int!) {
  vaults(first: 100, skip: $skip, where: { chainId_in: $chains },
         orderBy: TotalAssetsUsd, orderDirection: Desc) {
    items { address name chain { id } asset { symbol decimals }
            state { totalAssets totalAssetsUsd } }
  }
}"""

def q_vault_v2():
    return """query($address:String!, $chain:Int!) {
  vaultV2ByAddress(address: $address, chainId: $chain) {
    address name totalAssets totalAssetsUsd idleAssetsUsd
    asset { symbol decimals }
    %s
    %s
    %s
    adapters { items {
      address type
      ... on MorphoMarketV1Adapter {
        positions(first: 30) {
          %s
          items { state { supplyAssets supplyAssetsUsd } market { %s } }
        }
      }
      ... on MetaMorphoAdapter { metaMorpho { address name } }
    } }
  }
}""" % (opt("v2_owner"), opt("v2_curators"), opt("v2_caps"), opt("v2_pageinfo"), mf())

def q_vault_v1():
    return """query($address:String!, $chain:Int!) {
  vaultByAddress(address: $address, chainId: $chain) {
    address name asset { symbol decimals }
    state {
      totalAssets totalAssetsUsd %s
      allocation { supplyAssets supplyAssetsUsd %s market { %s } }
    }
  }
}""" % (opt("v1_roles"), opt("v1_cap"), mf())

# ---------------------------------------------------------------------------
# Normalisation: turn API responses into one common shape
# ---------------------------------------------------------------------------

PT_RE = re.compile(r"PT-.*-(\d{1,2}[A-Z]{3}\d{4})")

def pt_matured(symbol, today):
    if not symbol:
        return False
    m = PT_RE.search(symbol)
    if not m:
        return False
    try:
        return datetime.strptime(m.group(1).title(), "%d%b%Y").date() < today
    except ValueError:
        return False

def pt_days_left(symbol, today):
    """days until a PT collateral matures (None if not a PT)"""
    m = PT_RE.search(symbol or "")
    if not m:
        return None
    try:
        return (datetime.strptime(m.group(1).title(), "%d%b%Y").date() - today).days
    except ValueError:
        return None

def num(v):
    return float(v) if v is not None else 0.0

def norm_market(mkt, x_raw, x_usd, dec, cap_raw=None):
    st = mkt.get("state") or {}
    d = 10 ** dec
    rat = st.get("rateAtTarget")
    return {
        "market_id": mkt["marketId"],
        "collateral": (mkt.get("collateralAsset") or {}).get("symbol"),
        "coll_addr": ((mkt.get("collateralAsset") or {}).get("address") or "").lower(),
        "loan": (mkt.get("loanAsset") or {}).get("symbol"),
        "lltv": num(mkt.get("lltv")) / 1e18,
        "S": num(st.get("supplyAssets")) / d,
        "B": num(st.get("borrowAssets")) / d,
        "x": num(x_raw) / d,
        "x_usd": num(x_usd),
        "cap": (num(cap_raw) / d) if cap_raw is not None else None,
        "api_apy": num(st.get("supplyApy")),
        "reward_apr": sum(num(r.get("supplyApr")) for r in (st.get("rewards") or []) if r),
        "rat_apr": rat_to_apr(rat) if rat else None,
    }

def norm_v2(v, chain):
    dec = int(v["asset"]["decimals"])
    total = num(v["totalAssets"]) / 10 ** dec
    total_usd = num(v.get("totalAssetsUsd")) or None
    out = {"version": "V2", "chain": chain, "address": v.get("address", "?"), "name": v["name"],
           "asset": v["asset"]["symbol"], "total": total, "total_usd": total_usd,
           "idle_usd": num(v.get("idleAssetsUsd")), "markets": [], "notes": []}
    ids = set()
    if (v.get("owner") or {}).get("address"):
        ids.add(v["owner"]["address"].lower())
    for c in ((v.get("curators") or {}).get("items") or []):
        for a in c.get("addresses") or []:
            if a.get("address"):
                ids.add(a["address"].lower())
    out["ids"] = ids
    # ---- V2 caps: a market can only receive money if it has a market cap,
    # and every market sharing a collateral token also shares that cap
    out["coll_caps"], market_caps, out["has_caps"] = {}, {}, False
    total_raw = num(v["totalAssets"])
    for c in ((v.get("caps") or {}).get("items") or []):
        out["has_caps"] = True
        eff = min(num(c.get("absoluteCap")), num(c.get("relativeCap")) / 1e18 * total_raw) / 10 ** dec
        alloc = num(c.get("allocation")) / 10 ** dec
        dd = c.get("data") or {}
        if dd.get("__typename") == "MarketV1CapData":
            market_caps[((dd.get("marketParams") or {}).get("id") or "").lower()] = eff
        elif dd.get("__typename") == "CollateralCapData":
            out["coll_caps"][(dd.get("collateralAddress") or "").lower()] = [eff, alloc]
    for ad in (v.get("adapters") or {}).get("items", []):
        if "positions" in ad:
            pos = ad["positions"]
            pi = pos.get("pageInfo") or {}
            if pi.get("countTotal") and pi["countTotal"] > 30:
                out["notes"].append(f"adapter has {pi['countTotal']} markets, only 30 read")
            for p in pos.get("items", []):
                st = p.get("state") or {}
                mk_ = norm_market(p["market"], st.get("supplyAssets"), st.get("supplyAssetsUsd"), dec)
                if out["has_caps"]:
                    # no market cap entry = the curator hasn't enabled it
                    mk_["cap"] = market_caps.get(mk_["market_id"].lower(), 0.0)
                out["markets"].append(mk_)
        elif ad.get("metaMorpho"):
            out["notes"].append(f"invests via V1 vault {ad['metaMorpho']['name']} (not looked through)")
        else:
            out["notes"].append(f"adapter type {ad.get('type')} not analysed")
    return out

def norm_v1(v, chain):
    dec = int(v["asset"]["decimals"])
    st = v["state"]
    out = {"version": "V1", "chain": chain, "address": v.get("address", "?"), "name": v["name"],
           "asset": v["asset"]["symbol"], "total": num(st["totalAssets"]) / 10 ** dec,
           "total_usd": num(st.get("totalAssetsUsd")) or None, "idle_usd": 0.0,
           "markets": [], "notes": [],
           "ids": {a.lower() for a in (st.get("curator"), st.get("owner")) if a},
           "coll_caps": {}, "has_caps": True}
    for a in st.get("allocation") or []:
        mkt = a["market"]
        if not mkt.get("collateralAsset"):           # V1 "idle market"
            out["idle_usd"] += num(a.get("supplyAssetsUsd"))
            continue
        out["markets"].append(norm_market(mkt, a.get("supplyAssets"), a.get("supplyAssetsUsd"),
                                          dec, a.get("supplyCap")))
    return out

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

GENERIC_WORDS = {"usdc", "usdt", "eth", "weth", "the", "morpho", "vault", "steakhouse"}

def group_curators(vaults):
    """Link vaults run by the same curator: shared curator/owner address, or
    (when no address is available) the same first word of the vault name.
    Then record, for each market, how much the curator's OTHER vaults hold."""
    parent = list(range(len(vaults)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    def union(i, j):
        parent[find(i)] = find(j)
    seen = {}
    for i, v in enumerate(vaults):
        keys = set(v.get("ids") or ())
        if not keys:
            w = (v["name"].split() or ["?"])[0].lower()
            keys = {"name:" + w}
        for k in keys:
            if k in seen:
                union(i, seen[k])
            else:
                seen[k] = i
    groups = {}
    for i, v in enumerate(vaults):
        groups.setdefault(find(i), []).append(v)
    for members in groups.values():
        label = " / ".join(sorted({m["name"].split(" ")[0] for m in members}))
        for v in members:
            v["curator_group"] = label
            v["siblings"] = [m["name"] for m in members if m is not v]
        holdings = {}
        for v in members:
            for m in v["markets"]:
                key = (v["chain"], m["market_id"])
                holdings.setdefault(key, []).append((v["name"], m["x"]))
        for v in members:
            for m in v["markets"]:
                others = [(n, x) for n, x in holdings[(v["chain"], m["market_id"])]
                          if n != v["name"] and x > 0]
                m["x_sib"] = sum(x for _, x in others)
                m["sibling_vaults"] = "; ".join(
                    f"{n} {x / m['S'] * 100:.1f}%" for n, x in others
                    if m["S"] > 0 and x / m["S"] >= 0.001)

def analyse(v, args, today):
    price = (v["total_usd"] / v["total"]) if v["total_usd"] and v["total"] else 1.0
    v["price"] = price
    bump = BUMP_USD / price
    rows = []
    for m in v["markets"]:
        m["dead"] = m["S"] * price < args.dead_usd or pt_matured(m["collateral"], today)
        # frozen = 100% utilized with the rate pinned near the protocol max:
        # usually a market with stuck bad debt whose "supply" is phantom interest
        m["frozen"] = bool(m["S"] > 0 and m["B"] / m["S"] >= 0.995
                           and m["rat_apr"] and m["rat_apr"] >= 1.5)
        if m["frozen"]:
            m["dead"] = True
        if m["S"] <= 0:
            m.update(u=None, model_apy=None, marginal=None, marginal_g=None, share=0, share_g=0,
                     drift=None, model_ok=False)
            continue
        u = m["B"] / m["S"]
        m["u"] = u
        m["share"] = m["x"] / m["S"]
        m["share_g"] = (m["x"] + m.get("x_sib", 0.0)) / m["S"]
        m["avail_usd"] = (m["S"] - m["B"]) * price
        if m["rat_apr"]:
            m["model_apy"] = supply_apy(m["S"], m["B"], m["rat_apr"])
            m["borrow_apr"] = borrow_apr(u, m["rat_apr"])
            m["drift"] = drift_30d(u)
            # below the kink the model should match the API closely; a big gap
            # means a market fee or a non-adaptive IRM -> don't trust marginal
            tol = 0.002 if u <= TARGET_U else 0.01
            m["model_ok"] = abs(m["model_apy"] - m["api_apy"]) <= tol and not m["frozen"]
            # vault-only view (what we computed by hand)
            v0 = income(m, m["x"], group=False)
            v1 = income(m, m["x"] + bump, group=False)
            m["income"] = v0
            m["new_money_earns"] = bump * (supply_apy(m["S"] + bump, m["B"], m["rat_apr"])
                                           + m["reward_apr"] * m["S"] / (m["S"] + bump))
            m["existing_loses"] = m["new_money_earns"] - (v1 - v0)
            m["marginal"] = (v1 - v0) / bump
            # curator view: also counts the curator's other vaults in this market
            inc0 = income(m, m["x"])
            inc1 = income(m, m["x"] + bump)
            m["marginal_g"] = (inc1 - inc0) / bump
            # cost of taking $10k OUT (what the vault gives up per $ withdrawn)
            if m["x"] >= bump:
                m["withdraw_cost"] = (v0 - income(m, m["x"] - bump, group=False)) / bump
                newS = m["S"] - bump
                m["can_withdraw"] = (u <= args.u_ceiling and newS > 0
                                     and m["B"] / newS <= args.u_ceiling)
            else:
                m["withdraw_cost"], m["can_withdraw"] = None, False
        else:
            m.update(model_apy=None, marginal=None, marginal_g=None, drift=None, model_ok=False,
                     income=m["x"] * (m["api_apy"] + m.get("reward_apr", 0.0)))

    frozen = [m for m in v["markets"] if m.get("frozen") and m["x"] > 0]
    if frozen:
        fz = sum(m["x"] for m in frozen) * price
        v["notes"].append(f"FROZEN market(s) {', '.join(label(m) for m in frozen)} hold ${fz:,.0f}: "
                          "100% utilized at max rate - likely stuck bad debt; TVL and APY are not real")
    v["frozen_usd"] = sum(m["x"] for m in frozen) * price
    live = [m for m in v["markets"] if m["S"] > 0 and not m.get("frozen")]
    deployed = sum(m["x"] for m in live)
    v["deployed"] = deployed
    v["income"] = sum(m.get("income", 0) for m in live)
    real_total = v["total"] - v["frozen_usd"] / price
    v["apy_gross"] = v["income"] / real_total if real_total > 0 else 0

    # ---- signal 1: allocation gap (greedy, conservative) -------------------
    global REWARDS
    REWARDS = False
    g_native, _ = reallocate(v, args, price, include_idle=False)
    REWARDS = True
    v["alloc_gain_native_usd"] = g_native * price
    v["reward_income"] = sum(m["x"] * m.get("reward_apr", 0.0) for m in v["markets"]
                             if m["S"] > 0 and not m.get("frozen")) * price
    gain, moves = reallocate(v, args, price, include_idle=False)
    # ---- withdrawable liquidity: per market the vault can only pull
    # min(its own position, the market's unborrowed cash)
    def withdrawable(alloc, revert=False):
        """revert=True: assume borrowers absorb new supply until each market is
        back at its current utilization (the realistic case a few days later)"""
        tot = 0.0
        for m in live:
            a = alloc.get(m["market_id"], m["x"])
            S_new = m["S"] - m["x"] + a
            free = S_new * (1 - m["B"] / m["S"]) if revert else S_new - m["B"]
            tot += max(0.0, min(a, free))
        return tot * price + v["idle_usd"]
    for m in live:
        m["withdrawable_usd"] = max(0.0, min(m["x"], m["S"] - m["B"])) * price
    v["liq_now"] = withdrawable({})
    after = v.get("_alloc_after", {}).get("small", {})
    v["liq_after"] = withdrawable(after)
    v["liq_after_revert"] = withdrawable(after, revert=True)
    v["liq_now_revert"] = withdrawable({}, revert=True)
    v["alloc_gain_usd"] = gain * price
    v["alloc_gain_bps"] = gain / v["total"] * 1e4 if v["total"] else 0
    v["alloc_moves"] = moves
    pg, pmoves = reallocate(v, args, price, sources="dominant")
    v["pricing_gain_usd"] = pg * price
    v["pricing_moves"] = pmoves

    # cheapest source = funded market where withdrawing costs the least
    srcs = [m for m in live if m["x"] * price >= args.min_pos_usd and m.get("can_withdraw")
            and m.get("model_ok") and m["share_g"] < args.dominant_share]
    dests = [m for m in live if dest_ok(m, args, price)]
    v["worst_funded"] = min(srcs, key=lambda m: m["withdraw_cost"]) if srcs else None
    dests = [m for m in dests if m is not v["worst_funded"]
             and coll_room(v, m, {}) * price >= args.min_pos_usd]
    v["best_dest"] = max(dests, key=lambda m: m["marginal_g"]) if dests else None
    if v["best_dest"] and v["worst_funded"] and v["best_dest"]["marginal_g"] <= v["worst_funded"]["withdraw_cost"]:
        v["best_dest"] = None          # nothing worth moving to
    v["spread"] = (v["best_dest"]["marginal_g"] - v["worst_funded"]["withdraw_cost"]) \
        if v["worst_funded"] and v["best_dest"] else None

    # ---- signal 2: idle cash ------------------------------------------------
    v["idle_pct"] = v["idle_usd"] / v["total_usd"] if v["total_usd"] else 0
    if v["idle_pct"] > 0.25:
        v["notes"].append(f"{v['idle_pct']:.0%} of the vault is idle - new vault, wind-down, or "
                          "deliberate liquidity reserve? check its history before drawing conclusions")
    igain, imoves = reallocate(v, args, price, include_idle=True, idle_only=True)
    v["idle_gain_usd"] = igain * price
    v["idle_moves"] = imoves

    # ---- signal 3: dominant markets ----------------------------------------
    v["dominant"] = [m for m in live if m["share_g"] >= args.dominant_share and m["x"] * price >= args.min_pos_usd]

    # ---- kink discipline: how far funded markets sit from 90% utilization --
    fm = [m for m in live if m["x"] * price >= args.min_pos_usd]
    dep = sum(m["x"] for m in fm)
    if dep > 0:
        v["kink_dist"] = sum(m["x"] * abs(m["u"] - TARGET_U) for m in fm) / dep
        v["pct_above_92"] = sum(m["x"] for m in fm if m["u"] > 0.92) / dep
        v["pct_below_85"] = sum(m["x"] for m in fm if m["u"] < 0.85) / dep
    else:
        v["kink_dist"] = v["pct_above_92"] = v["pct_below_85"] = None

    # ---- signal 4: dead markets --------------------------------------------
    v["dead"] = [m for m in v["markets"] if m["dead"]]
    return v

def dest_ok(m, args, price):
    return (not m["dead"] and m.get("model_ok") and m.get("marginal_g") is not None
            and m["S"] * price >= args.min_dest_usd
            and (m["cap"] is None or (m["cap"] - m["x"]) * price >= args.min_pos_usd))

def coll_room(v, m, used):
    """remaining room under the collateral-token cap shared by all markets on that collateral"""
    cc = v.get("coll_caps", {}).get(m.get("coll_addr"))
    if not cc:
        return float("inf")
    return cc[0] - used.get(m["coll_addr"], cc[1])

def reallocate(v, args, price, include_idle=False, idle_only=False, sources="small"):
    """Greedy: repeatedly move one step from the lowest-marginal source to the
    highest-marginal destination while the spread exceeds args.min_spread.
    Conservative rules: never withdraw from a market if that would push its
    utilization above args.u_ceiling; respect V1 caps; ignore dead markets and
    markets where our rate model doesn't match the API."""
    mk = [m for m in v["markets"] if m["S"] > 0 and m.get("model_ok")]
    if not mk:
        return 0.0, []
    alloc = {m["market_id"]: m["x"] for m in mk}
    step = max(args.step_usd / price, v["total"] * 0.0005)
    idle_left = 0.0
    if include_idle:
        buffer_tok = v["total"] * args.idle_buffer
        idle_left = max(0.0, v["idle_usd"] / price - buffer_tok)
    moved = {}
    gain = 0.0
    used = {k: c[1] for k, c in v.get("coll_caps", {}).items()}
    for _ in range(args.max_steps):
        # cheapest source
        best_src, best_loss = None, None
        if idle_left >= step:
            best_src, best_loss = "IDLE", 0.0
        if not idle_only:
            for m in mk:
                a = alloc[m["market_id"]]
                if a < step:
                    continue
                # "small": only markets where the vault is a minority lender
                # (pure allocation). "dominant": only markets it controls
                # (that is a pricing lever, reported separately).
                if sources == "small" and m["share_g"] >= args.dominant_share:
                    continue
                if sources == "dominant" and m["share_g"] < args.dominant_share:
                    continue
                newS = m["S"] - m["x"] + a - step
                # conservative: don't withdraw from a market already above the
                # ceiling, or if the withdrawal would push it above
                if newS <= 0 or m["B"] / m["S"] > args.u_ceiling or m["B"] / newS > args.u_ceiling:
                    continue
                # conservative: a withdrawal gets no credit for pushing up the
                # rate on sibling vaults' money (that is the pricing lever)
                loss = (income(m, a, group=False) - income(m, a - step, group=False)) / step
                if best_loss is None or loss < best_loss:
                    best_src, best_loss = m, loss
        if best_src is None:
            break
        best_dst, best_gain = None, None
        for m in mk:
            if m is best_src or not dest_ok(m, args, price):
                continue
            a = alloc[m["market_id"]]
            if m["cap"] is not None and a + step > m["cap"]:
                continue
            if coll_room(v, m, used) < step:
                continue
            g = (income(m, a + step) - income(m, a)) / step
            if best_gain is None or g > best_gain:
                best_dst, best_gain = m, g
        if best_dst is None or best_gain - best_loss < args.min_spread:
            break
        if best_src == "IDLE":
            idle_left -= step; sname = "IDLE"
        else:
            alloc[best_src["market_id"]] -= step; sname = label(best_src)
            if best_src.get("coll_addr") in used:
                used[best_src["coll_addr"]] -= step
        alloc[best_dst["market_id"]] += step
        if best_dst.get("coll_addr") in used:
            used[best_dst["coll_addr"]] += step
        gain += (best_gain - best_loss) * step
        key = (sname, label(best_dst))
        moved[key] = moved.get(key, 0) + step
    moves = [(s, d, amt * price) for (s, d), amt in moved.items()]
    moves.sort(key=lambda t: -t[2])
    v.setdefault("_alloc_after", {})[sources if not include_idle else "idle"] = dict(alloc)
    return gain, moves

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def label(m):
    """collateral plus LLTV, so two wstETH markets don't look identical"""
    return f"{m['collateral']} ({m['lltv']*100:.1f}%)" if m.get("lltv") else str(m["collateral"])

def pct(x, d=2):
    return "" if x is None else f"{x*100:.{d}f}%"

HTML_CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--head:#f0f0eb;--accent:#2f5d8a}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--card:#1f1f1d;--line:#34332f;--head:#272724;--accent:#8cb4dd}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:22px 0 4px;color:var(--accent)}p{margin:6px 0}ul{margin:4px 0 8px;padding-left:20px}li{margin:2px 0}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 10px;border-bottom:1px solid var(--line);
text-align:left;white-space:nowrap}th{background:var(--head);position:sticky;top:0;font-weight:600}
tr:last-child td{border-bottom:none}td.n{text-align:right;font-variant-numeric:tabular-nums}
.note{color:var(--muted);font-size:13px}
"""

def md_to_html(md, title):
    """Tiny converter for the summary's own markdown (headings, tables, lists)."""
    def inline(t):
        t = html.escape(t)
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    out, lines, i = [], md.split("\n"), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")]); i += 1
            head, body = rows[0], [r for r in rows[1:] if not set("".join(r)) <= set("-: ")]
            out.append('<div class="tw"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>")
            for r in body:
                cells = "".join(
                    f'<td class="n">{inline(c)}</td>' if re.fullmatch(r"[-$\d.,%x pM]+", c) and re.search(r"\d", c) else f"<td>{inline(c)}</td>"
                    for c in r)
                out.append(f"<tr>{cells}</tr>")
            out.append("</tbody></table></div>"); continue
        if ln.startswith("- "):
            out.append("<ul>")
            while i < len(lines) and lines[i].startswith("- "):
                out.append(f"<li>{inline(lines[i][2:])}</li>"); i += 1
            out.append("</ul>"); continue
        if ln.startswith("### "): out.append(f"<h3>{inline(ln[4:])}</h3>")
        elif ln.startswith("## "): out.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "): out.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("*"): out.append(f'<p class="note">{inline(ln.strip("*"))}</p>')
        elif ln.strip(): out.append(f"<p>{inline(ln)}</p>")
        i += 1
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f"<style>{HTML_CSS}</style></head><body><main>{''.join(out)}</main></body></html>")

def write_outputs(vaults, outdir, args):
    vaults.sort(key=lambda v: -((v.get("alloc_gain_usd") or 0) + (v.get("idle_gain_usd") or 0)))
    with open(os.path.join(outdir, "vaults.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["rank", "version", "chain", "vault", "curator_group", "address", "asset", "tvl_usd",
                    "gross_apy_incl_rewards", "reward_income_usd_yr", "alloc_gain_usd_yr", "alloc_gain_bps",
                    "alloc_gain_excl_rewards_usd_yr", "marginal_spread",
                    "cheapest_source_market", "source_withdraw_cost", "best_dest_market", "best_dest_marginal",
                    "idle_usd", "idle_pct", "idle_redeploy_gain_usd_yr", "pricing_gain_usd_yr_if_borrowers_stay",
                    "withdrawable_now_usd", "withdrawable_after_moves_usd", "withdrawable_after_moves_once_borrowers_catch_up_usd",
                    "kink_distance_pp", "pct_deployed_above_92pct_util", "pct_deployed_below_85pct_util",
                    "n_dominant_markets", "n_dead_markets", "top_move", "sibling_vaults", "notes"])
        for i, v in enumerate(vaults, 1):
            wf, bd = v.get("worst_funded"), v.get("best_dest")
            top = v["alloc_moves"][0] if v.get("alloc_moves") else None
            w.writerow([i, v["version"], v["chain"], v["name"], v.get("curator_group", ""), v["address"], v["asset"],
                        round(v["total_usd"] or 0), pct(v["apy_gross"], 3), round(v.get("reward_income", 0)),
                        round(v["alloc_gain_usd"]), round(v["alloc_gain_bps"], 1), round(v.get("alloc_gain_native_usd", 0)),
                        pct(v["spread"]), label(wf) if wf else "", pct(wf["withdraw_cost"]) if wf else "",
                        label(bd) if bd else "", pct(bd["marginal_g"]) if bd else "",
                        round(v["idle_usd"]), pct(v["idle_pct"]), round(v["idle_gain_usd"]), round(v["pricing_gain_usd"]),
                        round(v.get("liq_now", 0)), round(v.get("liq_after", 0)), round(v.get("liq_after_revert", 0)),
                        round(v["kink_dist"] * 100, 2) if v["kink_dist"] is not None else "",
                        pct(v["pct_above_92"], 1), pct(v["pct_below_85"], 1),
                        len(v["dominant"]), len(v["dead"]),
                        f"{top[0]} -> {top[1]} ${top[2]:,.0f}" if top else "",
                        "; ".join(v.get("siblings", [])), "; ".join(v["notes"])])
    with open(os.path.join(outdir, "markets.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["vault", "version", "chain", "market_id", "collateral", "loan", "lltv",
                    "vault_supply_usd", "market_supply_usd", "market_borrow_usd", "vault_share",
                    "curator_share", "sibling_vaults_in_market", "cap_headroom_usd",
                    "cap_usd", "rate_at_target_apr", "utilization", "curve_factor", "borrow_apr",
                    "supply_apy_model", "supply_apy_api", "model_matches_api",
                    "reward_apr", "total_apy_incl_rewards",
                    "vault_withdrawable_usd", "vault_income_usd_yr", "new_10k_earns", "existing_loses", "marginal_return",
                    "marginal_return_curator", "withdraw_cost_curator",
                    "rate_drift_30d_x", "available_liquidity_usd", "dead"])
        for v in vaults:
            p = v["price"]
            for m in v["markets"]:
                if m["S"] <= 0:
                    continue
                w.writerow([v["name"], v["version"], v["chain"], m["market_id"], m["collateral"],
                            m["loan"], m["lltv"], round(m["x"] * p), round(m["S"] * p), round(m["B"] * p),
                            pct(m["share"]), pct(m.get("share_g")), m.get("sibling_vaults", ""),
                            (round(max(0.0, min(m["cap"] - m["x"], coll_room(v, m, {}))) * p)
                             if m["cap"] is not None else ""),
                            round(m["cap"] * p) if m["cap"] is not None else "",
                            pct(m["rat_apr"], 3), pct(m["u"], 3),
                            round(curve_factor(m["u"]), 4) if m["u"] is not None else "",
                            pct(m.get("borrow_apr"), 3), pct(m["model_apy"], 3), pct(m["api_apy"], 3),
                            m.get("model_ok"), pct(m.get("reward_apr"), 3),
                            pct((m["model_apy"] or m["api_apy"]) + m.get("reward_apr", 0.0), 3) if m.get("model_apy") is not None else pct(m["api_apy"] + m.get("reward_apr", 0.0), 3),
                            round(m.get("withdrawable_usd", 0)), round(m.get("income", 0) * p),
                            round(m.get("new_money_earns", 0) * p) if m["marginal"] is not None else "",
                            round(m.get("existing_loses", 0) * p) if m["marginal"] is not None else "",
                            pct(m["marginal"]), pct(m.get("marginal_g")), pct(m.get("withdraw_cost")), round(m["drift"], 2) if m.get("drift") else "",
                            round(m.get("avail_usd", 0)), m["dead"]])
    lines = [f"# Morpho allocation-efficiency scan — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             f"Vaults scanned: {len(vaults)} (TVL ${args.min_tvl:,.0f}"
             + (f" to ${args.max_tvl:,.0f}" if args.max_tvl else "+") + f", chains {args.chains})", "",
             "## Top vaults by money left on the table (allocation gap + idle redeploy)", "",
             "| # | Vault | TVL | Gap $/yr | Gap bps | Gap excl. rewards $/yr | Cheapest source (cost to withdraw) | Best destination (marginal) | Idle | Idle redeploy $/yr | Pricing $/yr* |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, v in enumerate(vaults[:args.top], 1):
        wf, bd = v.get("worst_funded"), v.get("best_dest")
        lines.append(f"| {i} | {v['name']} ({v['version']}, chain {v['chain']}) | ${(v['total_usd'] or 0)/1e6:,.1f}M | "
                     f"${v['alloc_gain_usd']:,.0f} | {v['alloc_gain_bps']:.1f} | ${v.get('alloc_gain_native_usd', 0):,.0f} | "
                     f"{label(wf) + ' ' + pct(wf['withdraw_cost']) if wf else '-'} | "
                     f"{label(bd) + ' ' + pct(bd['marginal_g']) if bd else '-'} | {pct(v['idle_pct'],1)} | "
                     f"${v['idle_gain_usd']:,.0f} | ${v['pricing_gain_usd']:,.0f} |")
    lines += ["", "*Pricing = extra income from withdrawing from markets the vault dominates, "
              "pushing utilization toward 90%. Only real if borrowers don't repay in response — treat as an upper bound."]
    # same gap ranked by bps, so small vaults with big inefficiencies surface
    by_bps = sorted([v for v in vaults if (v.get("alloc_gain_bps") or 0) > 0 and not v.get("frozen_usd")],
                    key=lambda v: -v["alloc_gain_bps"])
    lines += ["", "## Top vaults by gap in bps (size-independent)", "",
              "| # | Vault | Curator group | TVL | Gap bps | Gap $/yr | Gap excl. rewards $/yr | Top move |",
              "|---|---|---|---|---|---|---|---|"]
    for i, v in enumerate(by_bps[:args.top], 1):
        top = v["alloc_moves"][0] if v.get("alloc_moves") else None
        lines.append(f"| {i} | {v['name']} ({v['version']}, chain {v['chain']}) | {v.get('curator_group','')} | "
                     f"${(v['total_usd'] or 0)/1e6:,.1f}M | {v['alloc_gain_bps']:.1f} | ${v['alloc_gain_usd']:,.0f} | "
                     f"${v.get('alloc_gain_native_usd', 0):,.0f} | "
                     f"{(top[0] + ' -> ' + top[1] + ' $' + format(top[2], ',.0f')) if top else '-'} |")
    # kink discipline leaderboard
    kd = [v for v in vaults if v.get("kink_dist") is not None and not v.get("frozen_usd")]
    kd.sort(key=lambda v: -v["kink_dist"])
    lines += ["", "## Kink discipline — how far each vault's money sits from 90% utilization",
              "", "Lower is tighter. Automated allocators usually sit under 0.5 pp. "
              "Money above 92% means borrowers are paying steep rates (and will leave); "
              "money below 85% means the curve is oversupplied and rates are drifting down.", "",
              "| Vault | Curator group | TVL | Avg distance from 90% | Deployed >92% util | Deployed <85% util |",
              "|---|---|---|---|---|---|"]
    for v in kd:
        lines.append(f"| {v['name']} | {v.get('curator_group','')} | ${(v['total_usd'] or 0)/1e6:,.1f}M | "
                     f"{v['kink_dist']*100:.2f} pp | {pct(v['pct_above_92'],1)} | {pct(v['pct_below_85'],1)} |")
    # curator roll-up
    groups = {}
    for v in vaults:
        groups.setdefault(v.get("curator_group", v["name"]), []).append(v)
    lines += ["", "## By curator", "",
              "| Curator group | Vaults | TVL | TVL-weighted kink distance | Idle | Gap + idle $/yr |",
              "|---|---|---|---|---|---|"]
    rows = []
    for g, vs in groups.items():
        tvl = sum(v["total_usd"] or 0 for v in vs)
        kk = [v for v in vs if v.get("kink_dist") is not None]
        wk = (sum((v["total_usd"] or 0) * v["kink_dist"] for v in kk) / sum(v["total_usd"] or 0 for v in kk)) if kk else None
        idle = sum(v["idle_usd"] for v in vs)
        opp = sum((v["alloc_gain_usd"] or 0) + (v["idle_gain_usd"] or 0) for v in vs)
        rows.append((g, len(vs), tvl, wk, idle, opp))
    for g, n, tvl, wk, idle, opp in sorted(rows, key=lambda r: -r[2]):
        lines.append(f"| {g} | {n} | ${tvl/1e6:,.1f}M | {'' if wk is None else f'{wk*100:.2f} pp'} | "
                     f"${idle/1e6:,.1f}M | ${opp:,.0f} |")
    lines += ["", "## Details for the top vaults", ""]
    for v in vaults[:args.top]:
        lines.append(f"### {v['name']} — {v['address']} (chain {v['chain']})")
        if v.get("siblings"):
            lines.append(f"Same curator group as: {', '.join(v['siblings'])}")
        lines.append(f"TVL ${(v['total_usd'] or 0):,.0f}; gross APY {pct(v['apy_gross'],3)}; "
                     f"allocation gap ${v['alloc_gain_usd']:,.0f}/yr ({v['alloc_gain_bps']:.1f} bps)")
        today_ = datetime.now(timezone.utc).date()
        for s, d, amt in v["alloc_moves"][:5]:
            days = pt_days_left(d, today_)
            note = (f" — destination PT matures in {days} days, so the annualised gain only lasts that long"
                    if days is not None and days < 120 else "")
            lines.append(f"- move ${amt:,.0f} from {s} to {d}{note}")
        if v["alloc_moves"]:
            lines.append(f"- withdrawable liquidity: ${v['liq_now']:,.0f} now -> ${v['liq_after']:,.0f} right after these moves; "
                         f"once borrowers absorb the new supply: ${v['liq_after_revert']:,.0f} "
                         f"({(v['liq_after_revert'] - v['liq_now_revert']) / (v['total_usd'] or 1) * 100:+.1f}% of TVL vs today)")
        if v["idle_usd"] > 0:
            lines.append(f"- idle ${v['idle_usd']:,.0f} ({pct(v['idle_pct'],1)}); redeploying above a "
                         f"{args.idle_buffer:.0%} buffer would add ~${v['idle_gain_usd']:,.0f}/yr")
        for s_, d_, amt in v["pricing_moves"][:3]:
            lines.append(f"- pricing lever (upper bound): withdraw ${amt:,.0f} from {s_} (vault-dominated) into {d_}")
        for m in v["dominant"]:
            sib = f" (incl. siblings: {m['sibling_vaults']})" if m.get("sibling_vaults") else ""
            lines.append(f"- dominant: {label(m)} — curator holds {pct(m['share_g'],0)} of market{sib}, "
                         f"utilization {pct(m['u'],1)}, base rate x{m['drift']:.2f} in 30d if unchanged"
                         if m.get("drift") else f"- dominant: {m['collateral']} ({pct(m['share_g'],0)})")
        for m in v["dead"]:
            if not m.get("frozen"):
                lines.append(f"- dead/matured market still listed: {label(m)} (market ${m['S']*v['price']:,.0f})")
        for n in v["notes"]:
            lines.append(f"- note: {n}")
        lines.append("")
    fz = [v for v in vaults if v.get("frozen_usd")]
    if fz:
        lines += ["## Vaults holding frozen (bad-debt) markets — excluded from the leaderboards", ""]
        for v in fz:
            lines.append(f"- {v['name']} (chain {v['chain']}): ${v['frozen_usd']:,.0f} of reported "
                         f"${(v['total_usd'] or 0):,.0f} TVL sits in a frozen market")
        lines.append("")
    lines += ["## Read before pitching", "",
              "- Caps are respected: V1 supply caps, V2 market caps and V2 collateral caps "
              "(a V2 market with no cap entry is treated as not enabled). V2 adapter caps are not checked.",
              "- Borrowing is held fixed; gaps assume small moves over short periods.",
              "- Rewards (incentive tokens) are included: each market's reward budget is assumed fixed and shared "
              "pro-rata, valued at today's token price. Campaigns end and tokens can be illiquid, so check "
              "'Gap excl. rewards' too: if the two differ a lot, the finding depends on incentives.",
              "- Single snapshot: only trust gaps that appear across several daily runs.",
              "- Markets where the rate model doesn't match the API (fees, fixed-rate IRMs) are excluded from moves."]
    md = "\n".join(lines)
    with open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(outdir, "summary.html"), "w", encoding="utf-8") as f:
        f.write(md_to_html(md, "Morpho allocation scan"))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch_list(builder, key, chains, min_tvl, tvl_of):
    out, skip = [], 0
    while True:
        r = gql_with_fallback(builder, {"chains": chains, "skip": skip})
        items = r["data"][key]["items"]
        for it in items:
            tvl = tvl_of(it) or 0
            if tvl >= min_tvl:
                out.append(it)
        if len(items) < 100 or (items and (tvl_of(items[-1]) or 0) < min_tvl):
            break
        skip += 100
    return out

def main():
    ap = argparse.ArgumentParser(description="Morpho allocation-efficiency scanner")
    ap.add_argument("--chains", type=int, nargs="+", default=[1, 8453])
    ap.add_argument("--min-tvl", type=float, default=20_000_000)
    ap.add_argument("--max-tvl", type=float, default=None,
                    help="only REPORT vaults up to this TVL; larger vaults are still read so "
                         "curator/sibling shares stay correct")
    ap.add_argument("--skip-v1", action="store_true")
    ap.add_argument("--skip-v2", action="store_true")
    ap.add_argument("--u-ceiling", type=float, default=0.90,
                    help="never withdraw from a market if it pushes utilization above this")
    ap.add_argument("--min-spread", type=float, default=0.001, help="stop moving when spread < this (0.001 = 0.10%%)")
    ap.add_argument("--step-usd", type=float, default=25_000)
    ap.add_argument("--max-steps", type=int, default=4000)
    ap.add_argument("--idle-buffer", type=float, default=0.03, help="idle share kept for withdrawals")
    ap.add_argument("--min-dest-usd", type=float, default=500_000, help="ignore destination markets smaller than this")
    ap.add_argument("--min-pos-usd", type=float, default=50_000)
    ap.add_argument("--dead-usd", type=float, default=10_000)
    ap.add_argument("--dominant-share", type=float, default=0.5,
                    help="vault share of a market above which it counts as dominant")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--limit", type=int, help="only scan the N largest vaults (quick test)")
    ap.add_argument("--from-raw", help="re-analyse the raw/ folder of an earlier scan (no API calls)")
    ap.add_argument("--no-open", action="store_true", help="don't open summary.html in the browser when done")
    ap.add_argument("--offline-v2", help="analyse a saved vaultV2ByAddress JSON response instead of calling the API")
    ap.add_argument("--offline-chain", type=int, default=1)
    ap.add_argument("--offline-tvl-usd", type=float, help="vault TVL in USD for offline mode (defaults to totalAssets)")
    ap.add_argument("--offline-decimals", type=int, help="asset decimals if the saved query has no asset field (USDC 6, RLUSD 18)")
    args = ap.parse_args()
    today = datetime.now(timezone.utc).date()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    outdir = os.path.join("morpho_scan_out", stamp)
    os.makedirs(os.path.join(outdir, "raw"), exist_ok=True)

    vaults = []
    if args.offline_v2:
        raw = json.load(open(args.offline_v2))
        v = raw["data"]["vaultV2ByAddress"]
        if args.offline_tvl_usd:
            v["totalAssetsUsd"] = args.offline_tvl_usd
        if "asset" not in v:
            if args.offline_decimals is None:
                sys.exit("saved query has no asset field: pass --offline-decimals")
            v["asset"] = {"symbol": "?", "decimals": args.offline_decimals}
        n = norm_v2(v, args.offline_chain)
        if not n["total_usd"]:
            n["total_usd"] = n["total"]
        vaults.append(n)
    elif args.from_raw:
        for fn in sorted(os.listdir(args.from_raw)):
            if not fn.endswith(".json"):
                continue
            ver, chain = fn.split("_")[0], int(fn.split("_")[1])
            r = json.load(open(os.path.join(args.from_raw, fn)))
            n = norm_v2(r["data"]["vaultV2ByAddress"], chain) if ver == "V2" else \
                norm_v1(r["data"]["vaultByAddress"], chain)
            if n["markets"]:
                vaults.append(n)
    else:
        targets = []
        if not args.skip_v2:
            print("Listing V2 vaults...")
            for it in fetch_list(q_list_v2, "vaultV2s", args.chains, args.min_tvl,
                                 lambda it: it.get("totalAssetsUsd")):
                targets.append(("V2", it["address"], it["chain"]["id"], it["name"],
                                it.get("totalAssetsUsd") or 0))
        if not args.skip_v1:
            print("Listing V1 vaults...")
            for it in fetch_list(q_list_v1, "vaults", args.chains, args.min_tvl,
                                 lambda it: (it.get("state") or {}).get("totalAssetsUsd")):
                targets.append(("V1", it["address"], it["chain"]["id"], it["name"],
                                (it.get("state") or {}).get("totalAssetsUsd") or 0))
        print(f"{len(targets)} vaults above ${args.min_tvl:,.0f}")
        targets.sort(key=lambda t: -t[4])
        if args.limit:
            targets = targets[:args.limit]
        for i, (ver, addr, chain, name, _tvl) in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {ver} {name}")
            try:
                if ver == "V2":
                    r = gql_with_fallback(q_vault_v2, {"address": addr, "chain": chain})
                    v = r["data"]["vaultV2ByAddress"]
                    n = norm_v2(v, chain)
                else:
                    r = gql_with_fallback(q_vault_v1, {"address": addr, "chain": chain})
                    v = r["data"]["vaultByAddress"]
                    n = norm_v1(v, chain)
                with open(os.path.join(outdir, "raw", f"{ver}_{chain}_{addr}.json"), "w", encoding="utf-8") as f:
                    json.dump(r, f)
                if not n["markets"]:
                    print("   no direct market positions, skipped"); continue
                vaults.append(n)
            except Exception as e:
                print(f"   skipped: {e}")
    group_curators(vaults)
    done = []
    for n in vaults:
        try:
            done.append(analyse(n, args, today))
        except Exception as e:
            print(f"   analysis skipped for {n.get('name')}: {type(e).__name__}: {e}")
    vaults = done
    if args.max_tvl:
        before = len(vaults)
        vaults = [v for v in vaults if (v["total_usd"] or 0) <= args.max_tvl]
        print(f"Reporting {len(vaults)} of {before} vaults with TVL <= ${args.max_tvl:,.0f}")
    write_outputs(vaults, outdir, args)
    page = os.path.abspath(os.path.join(outdir, "summary.html"))
    print(f"\nDone. Results in {outdir}/  (open summary.html in your browser)")
    if not args.no_open:
        try:
            webbrowser.open("file://" + page)
        except Exception:
            pass

if __name__ == "__main__":
    main()
