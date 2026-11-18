# Risk Engine

`risk_engine.py` — a curator-wide risk monitor for Morpho vaults.

For every vault a curator runs, it answers four questions:

1. **Liquidity** — if the largest depositor wants out today, can they get out?
2. **Oracles** — is the oracle price close to what the collateral actually sells for?
3. **Liquidations** — if prices fall, can on-chain liquidators clear the debt in time?
4. **Borrowers** — when a market maxes out, how does it recover?

---

## Usage

```bash
python3 risk_engine.py --curator sentora              # one curator, all modules
python3 risk_engine.py --curator all                  # every configured curator
python3 risk_engine.py --curator gauntlet --max-vaults 5   # quick test: 5 largest vaults
python3 risk_engine.py --curator galaxy --modules runs oracle
python3 risk_engine.py --list-curators
```

| Flag | Default | Meaning |
|---|---|---|
| `--curator` | `sentora` | Curator key from `CURATORS`, or `all` |
| `--modules` | all four | Any of `runs oracle liquidation borrower` |
| `--chains` | `1 8453 42161 130 137` | Chain IDs to scan |
| `--min-tvl` | `1000000` | Skip vaults smaller than this (USD) |
| `--max-vaults` | – | Only the N largest vaults per curator |
| `--borrowers` | `100` | Largest borrowers read per market |
| `--history-days` | `30` | History window for the borrower module |
| `--no-open` | off | Don't open the report in a browser |

## Output

`risk_out/<curator>_<timestamp>/`

| File | Contents |
|---|---|
| `report.html` / `report.md` | Key findings, then one section per module |
| `vaults.csv` | One row per vault: TVL, withdrawable, top depositors, exit gap, drained APR, flag |
| `depositors.csv` | Top 5 depositors per vault with labels |
| `markets.csv` | One row per market: every oracle, liquidation and borrower metric |
| `raw.json` | Every API response used, for audit |

**Flags:** `RED` needs attention · `AMBER` worth watching · `OK` fine · `INFO` context, not risk · `n/a` couldn't compute.
Key findings only include markets with at least $5M or 0.5% of the curator's TVL.

---

## Module 1 — Liquidity and depositors

| Metric | Definition |
|---|---|
| **Withdrawable now** | Σ over markets of min(vault's position, market's unborrowed cash) + idle cash |
| **Largest depositor / Top 5** | Share of vault TVL held by the largest / five largest wallets |
| **Exit gap** | Largest depositor's balance − withdrawable now: what they could not take out today |
| **Borrow APR once drained** | 4 × rate-at-target (the AdaptiveCurveIRM rate at 100% utilization). It keeps doubling roughly every 5 days while utilization stays at 100% |

Flag: `RED` if the exit gap exceeds 15% of TVL, `AMBER` if positive. A vault where one depositor holds ≥90% is `INFO` — a single-client mandate, not run risk.

**Linked wallets** lists depositors that appear in several of the curator's vaults, or that also borrow from the curator's own markets. Depositors that are themselves Morpho vaults are named.

## Module 2 — Oracles

| Metric | Definition |
|---|---|
| **Prices collateral via** | The oracle's feeds and vaults, read from the Morpho API (e.g. `BTC / USD`, `vault rate × USDe / USD`) |
| **Kind** | `collateral market price` · `underlying price` (e.g. kBTC priced off BTC) · `exchange rate` · `fixed` · `unknown` |
| **Oracle price** | Morpho oracle `price / 1e36 × 10^collDec / 10^loanDec` — loan tokens per collateral token |
| **DEX price** | KyberSwap quote for selling $10K of collateral into the loan token |
| **Gap** | DEX price ÷ oracle price − 1. Negative = oracle overvalues the collateral |
| **Buffer** | 1 − LLTV |

Flag `RED` if the gap is larger than the buffer, if there is no DEX route, or if `(1 + bonus) × (1 + gap) < 1` — i.e. even a $10K liquidation loses money.

## Module 3 — Liquidations

Built from each borrower's health factor (HF = collateral value × LLTV ÷ debt). A price drop of `1 − 1/HF` makes that borrower liquidatable.

| Metric | Definition |
|---|---|
| **Top borrower** | Largest borrower's share of the market's debt |
| **Min HF** | Lowest health factor (can be a dust position — read cliffs instead) |
| **Cliff 25% / 50%** | Price drop at which 25% / 50% of the market's debt becomes liquidatable |
| **Capacity → loan / → USDC** | Largest DEX sale (tested at $100K, 300K, 1M, 3M, 10M, 30M, 100M) where `LIF × sale price ÷ oracle price > 1`, i.e. the liquidator still profits. Measured into the loan token and into USDC to separate collateral liquidity from loan-token liquidity |
| **Shock rows** | −10/20/30% for unrelated assets, −2/5/10% for same-family pairs (inferred from symbols) |
| **Liquidatable debt** | Σ debt of borrowers with HF × (1 − shock) < 1 |
| **Collateral to sell** | Σ their collateral × oracle price × (1 − shock) |
| **Trades (loan / USDC)** | Collateral to sell ÷ capacity — back-to-back max-size liquidations needed |

Liquidation incentive (Morpho Blue): `LIF = min(1.15, 1 / (0.3 × LLTV + 0.7))`. Bonus = LIF − 1 (4.38% at 86% LLTV, 2.62% at 91.5%, 7.41% at 77%).

Flag: `RED` if more than 10 trades are needed on both routes, `AMBER` if more than 1.

## Module 4 — Borrowers and rates

| Metric | Definition |
|---|---|
| **Util now / Borrow APY now** | Current utilization and borrow rate |
| **Time >92%** | Share of the history window spent well above the 90% kink |
| **Spikes** | Episodes above 92% utilization, ending when utilization is back under 90.5% |
| **Median spike** | Typical spike length (hours) |
| **Borrow change / Supply change** | Median change in borrowing and supply during spikes — negative borrow = borrowers repaid; positive supply = new money (often the curator reallocating) ended the spike |

Flag: `RED` if a market has sat above 92% for more than 3 days, `AMBER` if it spends over a fifth of its time there. A rate-sensitivity regression is kept in `markets.csv` but not shown — on hourly data it mostly captures borrowing driving the rate, not the rate deterring borrowers.

---

## Limitations

- Snapshot data; rerun before quoting numbers.
- On-chain DEX liquidity only (KyberSwap). Issuer redemption and OTC routes are not counted.
- Borrowers are assumed passive under shocks; only the 100 largest positions are read.
- Oracle classification depends on the Morpho API's oracle schema, read via introspection. If a market shows `unknown`, check `raw.json`.
- Curator detection is by vault-name keywords.
