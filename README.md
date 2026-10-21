# Traders@SMU: Statistical Arbitrage (Fall 2026)

Research and development repository for the Traders@SMU Statistical Arbitrage team, Fall 2026.

**Portfolio Manager:** Davis Lynn ([@dplynn](https://github.com/dplynn))

## Semester focus: Kalman-filter hedging of ETFs

This semester's group strategy uses **Kalman-filter hedging to analyze ETFs**. Each ETF (a sector SPDR, or a sub-industry ETF) is hedged against its parent (SPY, its sector, or both) with a time-varying hedge ratio. The goals are to isolate a low-beta residual return and to find out whether that residual can be traded profitably after costs.

The group strategy is in active development, and this README will be updated as it progresses.

## Research to date

### Residual beta study: Kalman vs. static and rolling hedges ([`PCA/BetaResearch/`](PCA/BetaResearch/Re__ETF_List/summary.html))
*Contributed by Bryson Frazee-Duffin.*

31 ETF pairs (SPY, the 11 sector SPDRs, 20 sub-industry ETFs), Alpaca SIP bars at 1, 5 and 15 minutes and daily, 2016-2026. All parameters were fitted on 2016-2022 and tested once on 2023-2026.

- **Hedging works at every bar size.** A Kalman hedge with per-session drift keeps residual beta within ±0.02 for every sector intraday. A refreshed hedge removes market beta and a fixed one does not: sector betas moved 30-60% over four years. Intraday, the Kalman filter beats every rolling window, and at daily frequency they tie.
- **Sub-industry ETFs must be hedged against SPY and their sector together.** Hedging against the sector alone leaves +0.1 to +0.5 of market beta.
- **The return half fails after costs.** Daily residuals are white noise. 5- and 15-minute residuals mean-revert robustly, but the move is worth less than 1 bp per unit traded, which is below the cheapest half-spread. Settings tuned in-sample decay out of sample.
- **Next step:** a maker-execution study at 5-15 minutes, where the reversion is worth about 0.6-1.5 bp per unit and could only be captured with passive fills.

Reports: [`summary.html`](PCA/BetaResearch/Re__ETF_List/summary.html), [`phase_a_report.html`](PCA/BetaResearch/Re__ETF_List/phase_a_report.html) (beta), [`phase_b_report.html`](PCA/BetaResearch/Re__ETF_List/phase_b_report.html) (returns), [`grid_search.html`](PCA/BetaResearch/Re__ETF_List/grid_search.html).

### SPY basket reconstruction ([`Mean Reversion/`](Mean%20Reversion/README.md))
*Contributed by Jacob.*

Rebuilds SPY's price from its 500 constituents and from the 11 Select Sector SPDRs over 126 sessions (March-September 2026). The constituent basket tracks SPY with 0.9936 correlation and 1.53% annualized tracking error. The sector-ETF basket shows 2.66% tracking error, which is traced to the Select Sector 24/50 capping rule. Open `Mean Reversion/index.html` for the interactive study.

## Repository layout

```
.
├── Mean Reversion/            # SPY basket reconstruction study (offline HTML + data)
├── PCA/
│   └── BetaResearch/          # Kalman / OLS hedging and residual-return reports
├── AI.md, CLAUDE.md           # Coding standards for AI-assisted development
├── approved-packages.md       # Allowed Python packages
├── .claude/, opencode.json    # Guardrails that block destructive git/filesystem commands
└── README.md
```

## Contributing

All code follows [`AI.md`](AI.md):
- Python with `uv` and a `pyproject.toml`
- Test-driven development, with at least one test per feature
- Feature branches and pull requests only; never commit directly to `main`
- PEP 8, with all settings in `config.json` and all secrets in `.env` (with a blank `example.env`)
- New packages require approval. See [`approved-packages.md`](approved-packages.md).
