# Liquidation Engine

`liquidation_engine.py` — a deep-dive on liquidations across every Morpho market a curator lends to.

A position can be technically liquidatable but economically unattractive. This engine asks the question a lender actually cares about:

> **When a position becomes liquidatable, would a rational liquidator execute — and how much of the debt gets cleared?**

It is built as modules that share one market context (market data, borrowers, DEX price curve), so new modules plug in without rewrites.

| # | Module | Status |
|---|---|---|
| 1 | Liquidator profitability | ✅ built |
| 2 | Dynamic liquidity degradation | planned |
| 3 | Borrower concentration & liquidation clustering | planned |
| 4 | Oracle vs. executable price divergence | planned |
| 5 | Bad debt & residual debt simulator | planned |
| 6 | Liquidation throughput | planned |
| 7 | Cross-market contagion | planned |

---

## Usage

```bash
python3 liquidation_engine.py                       # every market of every configured curator
python3 liquidation_engine.py --curator sentora     # one curator
python3 liquidation_engine.py --mev-share 0.8       # different MEV assumption
```

| Flag | Default | Meaning |
|---|---|---|
| `--curator` | `all` | Curator key from `CURATORS` in `risk_engine.py`, or `all` |
| `--chains` | `1 8453 42161 130 137` | Chain IDs |
| `--min-tvl` | `1000000` | Skip vaults smaller than this (USD) |
| `--min-exposure` | `0` | Skip markets where the curator lends less than this (default: every market) |
| `--borrowers` | `100` | Largest borrowers read per market |
| `--mev-share` | `0.60` | Share of net profit paid to block builders |
| `--liq-call-gas` | `200000` | Gas for Morpho `liquidate()` + callback, on top of the swap's gas |
| `--stress-gas-mult` | `10` | Crash scenario gas multiplier |
| `--stress-gas-floor` | `50` | Minimum crash gas on Ethereum (gwei) |
| `--no-open` | off | Don't open the report in a browser |

A full run across five curators makes roughly 8 KyberSwap quotes per market; expect 15–30 minutes. Markets shared by several curators are only quoted once.

## Output

`liq_out/<timestamp>/`

| File | Contents |
|---|---|
| `index.html` | One row per curator: markets, amount lent, RED/AMBER counts, lent in RED markets |
| `<curator>/report.html` / `report.md` | Full report |
| `<curator>/profitability.csv` | One row per market: gas, min/optimal/max size, profit breakdown |
| `<curator>/shocks.csv` | One row per market × shock |
| `<curator>/positions.csv` | One row per liquidatable borrower × shock |
| `<curator>/price_curves.csv` | Executable/oracle price ratio at each quoted size |
| `raw.json` | API responses and quotes |

---

## Module 1 — Liquidator profitability

### Liquidator model

The baseline liquidator is an **atomic on-chain bot**. Morpho Blue transfers the seized collateral first and pulls the repayment at the end of the same transaction (`onMorphoLiquidate` callback), so the bot swaps collateral → loan token inside the transaction. It needs no capital and no flash loan: **financing cost = 0**.

### The profit formula

For a liquidation that repays debt `r`:

```
collateral seized V = r × LIF                       (valued at the oracle price)
proceeds            = V × executable price / oracle price
net profit          = proceeds − r − gas            (before MEV)
```

where `LIF = min(1.15, 1 / (0.3 × LLTV + 0.7))`.

The report splits net profit into parts that add up exactly:

| Component | Formula |
|---|---|
| Liquidation bonus | V − V / LIF |
| − Oracle/DEX gap | V × (1 − small-trade price / oracle) — includes pool fees |
| − Swap slippage | V × (small-trade price − size price) / oracle |
| − Gas | (swap gas from KyberSwap + liquidate call gas) × gas price × ETH price |
| **= Net profit** | before MEV |
| − MEV bribe | `--mev-share` × net profit |
| **= Liquidator take** | what the bot keeps |

A liquidation **happens** if net profit before MEV is positive — competing bots bid the rest away to builders, but someone still executes.

### Profit curve

KyberSwap is quoted for selling $10K, $100K, $300K, $1M, $3M, $10M, $30M and $100M of collateral (stopping once clearly unprofitable). The executable/oracle ratio is interpolated on a log scale and profit is evaluated on a fine grid:

| Metric | Definition |
|---|---|
| **Min repay** | Smallest liquidation where the bonus covers gas |
| **Optimal repay** | Size with the highest absolute profit |
| **Max repay** | Largest size still profitable before slippage eats the bonus (`≥` = still profitable at the largest quote) |
| **Margin** | Net profit ÷ debt repaid (ROI on capital is undefined — no capital is used) |

Both a **live gas** scenario (KyberSwap's current gas price) and a **crash gas** scenario (`--stress-gas-mult` × live, floored at `--stress-gas-floor` gwei on Ethereum) are computed.

### Under price shocks

Shocks: −10/20/30% for unrelated assets, −2/5/10% for same-family pairs.

| Metric | Definition |
|---|---|
| **Liquidatable debt** | Σ debt of borrowers with HF × (1 − shock) < 1 |
| **Underwater** | Debt left even if all collateral is seized: if collateral < debt × LIF, the liquidator takes everything and repays collateral ÷ LIF |
| **Profitable** | Share of liquidatable debt in positions large enough for the bonus to cover gas (each position seen alone) |
| **One block** | min(Σ seizable collateral, max profitable trade) ÷ LIF — what clears at a profit immediately, when all liquidations share the same DEX liquidity |
| **Dust** | Positions too small to liquidate at a profit |

### Flags

`RED` if no size is profitable even with crash gas, or if less than 25% of the mid-shock liquidatable debt clears in one block. `AMBER` if less than 100%. `OK` otherwise.

### Assumptions to keep in mind

- **Best case on liquidity.** Under a shock, the oracle and the DEX curve move together and slippage in % stays the same. Real liquidity usually thins in a crash — module 2 will model that.
- On-chain DEX liquidity only; issuer redemption and OTC are excluded by design (atomic bot).
- Pre-liquidation contracts are detected and listed per market, but their separate incentive terms are not yet modelled.
- Borrowers are assumed passive; the 100 largest positions are read.
