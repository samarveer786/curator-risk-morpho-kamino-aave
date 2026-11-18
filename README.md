# Morpho Curator Risk Toolkit

Open-source risk monitoring for **Morpho vault curators**. Point it at a curator (Sentora, Steakhouse, Gauntlet, RockawayX, Galaxy, or your own) and it reads every vault, every market and every large borrower from live on-chain data, then tells where the liquidity, oracle and liquidation risk actually sits — in plain-English HTML reports with CSVs behind every number.

Two engines, one shared library, **pure Python standard library — no installs, no API keys.**

| Engine | Question it answers | Docs |
|---|---|---|
| **Risk engine** (`risk_engine.py`) | Across all of a curator's vaults: can the biggest depositors get out, are the oracles honest, can liquidations clear a crash, and how do borrowers react to rates? | [docs/RISK_ENGINE.md](docs/RISK_ENGINE.md) |
| **Liquidation engine** (`liquidation_engine.py`) | For every market a curator lends to: when a position becomes liquidatable, would a rational liquidator actually do it — and how much of the debt gets cleared? | [docs/LIQUIDATION_ENGINE.md](docs/LIQUIDATION_ENGINE.md) |

`morpho_scan.py` is the shared Morpho API / interest-rate-model library both engines import. It also runs on its own as an allocation-efficiency scanner.

---

## Quick start

Requires Python 3.8+. Nothing to install.

```bash
git clone https://github.com/samarveer786/morpho-curator-risk.git
cd morpho-curator-risk

# Risk engine: one curator, all four modules
python3 risk_engine.py --curator sentora

# Liquidation engine: every market of every configured curator
python3 liquidation_engine.py
```

Each run writes a timestamped folder and opens the HTML report in your browser:

```
risk_out/sentora_20261118_1130/report.html
liq_out/20261118_1130/index.html          # summary across curators
liq_out/20261118_1130/sentora/report.html # per-curator detail
```

Keep all three `.py` files in the same folder — the engines import each other.

---

## Engine summaries

### Risk engine — curator-wide health check

Four modules, run per curator:

1. **Liquidity & depositors** — largest depositors vs. what can actually be withdrawn right now (the *exit gap*), the borrow rate the IRM would charge once a vault is drained, and linked wallets (the same address across several vaults, or depositing *and* borrowing in the curator's own markets).
2. **Oracles** — what each oracle actually prices from (collateral feed, underlying feed, or an exchange rate), and the gap between the oracle and the executable DEX price. Flags markets where even a small liquidation would lose money.
3. **Liquidations** — from real borrower health factors: the price drop at which 25% / 50% of a market's debt becomes liquidatable (*cliffs*), DEX capacity per profitable liquidation (into the loan token and into USDC), and how many back-to-back liquidations a shock would need.
4. **Borrowers & rates** — utilization spikes above the kink, how long they last, and whether they end by borrowers repaying or by new supply arriving.

### Liquidation engine — liquidator economics

Module 1 (built) — **Liquidator profitability**. Models an atomic on-chain liquidation bot (Morpho's callback means no capital or flash loan is needed) and, per market, measures:

- Net profit = bonus − oracle/DEX gap − swap slippage − gas, with an assumed 60% MEV bribe on top
- Minimum profitable size (gas-bound), optimal size, maximum profitable size (slippage-bound)
- Under −10/20/30% shocks (or −2/5/10% for correlated pairs): how much liquidatable debt is profitable, how much clears in **one block**, how many positions are dust, and how much is underwater

Live and crash-gas scenarios are reported side by side.

Roadmap: liquidity degradation · borrower concentration & clustering · oracle vs. executable divergence · bad-debt simulator · liquidation throughput · cross-market contagion.

---

## Data sources

| Source | Used for | Key needed |
|---|---|---|
| [Morpho GraphQL API](https://api.morpho.org/graphql) | Vaults, markets, positions, oracles, history | No |
| [KyberSwap Aggregator API](https://docs.kyberswap.com/) | Executable DEX prices, slippage curves, gas estimates | No |

Supported chains by default: Ethereum (1), Base (8453), Arbitrum (42161), Unichain (130), Polygon (137). Override with `--chains`.

## Adding a curator

Curators are matched by vault name. Edit `CURATORS` near the top of `risk_engine.py`:

```python
CURATORS = {
    "sentora": {"label": "Sentora", "keywords": ["sentora", "paypal usd main"]},
    "mycurator": {"label": "My Curator", "keywords": ["my curator"], "exclude": ["not my curator"]},
}
```

Both engines pick it up automatically.

## Repository layout

```
morpho_scan.py          shared Morpho API + AdaptiveCurveIRM library; standalone allocation scanner
risk_engine.py          curator risk engine (4 modules)
liquidation_engine.py   liquidation engine (module 1: liquidator profitability)
docs/RISK_ENGINE.md
docs/LIQUIDATION_ENGINE.md
```

## Caveats

- Every report is a **snapshot**. DEX liquidity, health factors and depositor balances move daily — rerun before quoting a number.
- Liquidation capacity counts **on-chain DEX liquidity only**. Issuer redemptions and OTC desks add capacity that isn't measured here.
- Borrowers are assumed passive under shocks (no top-ups or repayments); the 100 largest positions per market are read.
- This is research tooling, not financial advice.

## License

MIT — see [LICENSE](LICENSE).
