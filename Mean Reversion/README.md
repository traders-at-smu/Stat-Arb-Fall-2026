# Rebuilding SPY's share price from its parts

An interactive study of how closely SPY can be reconstructed from (a) the 500 S&P 500
constituent equities and (b) the eleven Select Sector SPDR ETFs, over 126 trading
sessions from 20 March to 18 September 2026.

Download and open `index.html` — it runs fully offline, no server needed. If GitHub Pages
is enabled for this repo it also serves at
`https://traders-at-smu.github.io/Stat-Arb-Fall-2026/spy-basket-tracking/`.

## Result

| Series | End price | Return | Ann. vol | Corr vs SPY | Ann. TE | Max gap |
|---|---|---|---|---|---|---|
| SPY price | $761.69 | +17.44% | 13.49% | 1.0000 | — | — |
| From 500 equities | $761.96 | +17.48% | 13.56% | 0.9936 | 1.53% | $4.84 |
| From sector ETFs | $758.77 | +16.99% | 13.52% | 0.9806 | 2.66% | $12.30 |

Both baskets are market-cap weighted with a divisor — the way the index itself is
computed — and both start at SPY's actual 20 March close of $648.57. Everything is on a
price basis, since SPY's quoted price excludes dividends.

## The headline finding

The eleven Select Sector SPDRs contain every S&P 500 constituent, so holding them at
index weights *should* reproduce the index. It doesn't, and the reason is structural.

Regrouping the 500 equities into 11 sectors and cap-weighting the sectors' own returns
reproduces the all-equity basket **exactly** (correlation 1.000000, 0.000% tracking
error) — an algebraic identity that rules out any weighting error. Swap the underlying
sector returns for the ETFs' actual returns and the level drops $2.92.

The cause is the Select Sector **24/50 capping rule**: no company above 24%, and if
everything above 4.8% together exceeds 50% of the index, it is all scaled down.
Measured against the stocks each fund actually holds:

| ETF | Beta vs own stocks | ETF vol | Underlying vol |
|---|---|---|---|
| XLC | **0.598** | 17.90% | 26.18% |
| XLY | 0.829 | 20.88% | 24.37% |
| XLK | 1.104 | 30.31% | 27.00% |
| others | 0.96 – 1.01 | — | — |

XLC moves barely half as much as the Communication Services stocks it holds, because
Alphabet and Meta blow through the 50% rule. This is **not arbitrageable** — the funds
are required to hold different weights than the index does, so capturing the gap needs
single-stock overlays to undo each cap.

## Files

| Path | What it is |
|---|---|
| `index.html` | The full interactive page. Self-contained apart from `contrib_data.js`. |
| `contrib_data.js` | Daily levels, sector weights, and per-security weights/returns (1.2 MB). |
| `data/daily_levels.csv` | The three price series, 126 rows. Derived aggregate output. |

The page has four tabs: charts (with per-series toggles), a daily price table where
every figure drills down into the securities behind it, daily sector weights, and the
capping analysis.

## Method notes

- **Divisor construction.** Each day's basket return is the prior-day-cap-weighted mean
  of member price returns, chained. Share-count changes (buybacks, ETF creation and
  redemption) are absorbed by the divisor rather than read as price moves — XLB's shares
  rose 23.8% and XLP's fell 13.2% over the window, so this matters.
- **Contribution decomposition.** An index level is total market cap over a divisor, so
  security *i* accounts for `level × weight(i)` dollars of it. Contributions sum to the
  level, not to the day's change.
- **Spin-off correction.** Honeywell spun off HONA on 29 June; the vendor's adjustment
  factor and share count both reset, which makes a naive adjusted-price chain read a
  −50.95% return. Index methodology absorbs spin-offs through the divisor, so that
  security-day is excluded. Seven ordinary splits in the window are handled correctly.
- **Membership** is rebuilt every session (504–507 names). Exact 2026 index removal dates
  are not recoverable from the available data, so the thirteen departing names are
  retired against the nine known addition dates; their combined weight is under 0.1%.
- **Known approximation.** The S&P 500 weights by float-adjusted market cap; the source
  supplies total shares outstanding. The resulting top-10 concentration of 38.53% is in
  line with the index's own, so the effect appears minor. The residual 1.53% tracking
  error is concentrated in three sessions (0.90% excluding them) and reverses day to day
  — isolated pricing noise rather than a weighting bias.

## Data source and licensing

Source data is Compustat Security Daily, accessed through Wharton Research Data Services
(WRDS) under a university subscription.

> **Before making this repository public:** raw and lightly-derived Compustat data is
> licensed and generally may not be redistributed. `data/daily_levels.csv` is an
> aggregate of 126 index levels and is the safest thing to share. `contrib_data.js`
> contains per-security daily weights and returns for 516 securities, which is much
> closer to redistributing the underlying vendor extract. If this repo is going public,
> check with your institution's WRDS representative first, or ship only
> `daily_levels.csv` and have users supply their own extract.

## Reproducing

The page reads a single file, `contrib_data.js`, of the form:

```js
window.DATA = {
  dates:[...], tickers:[...], tsec:[...], names:[...], order:[...],
  spy:[...], levSec:[...], levStk:[...],
  secW:[[...]], secR:[[...]], eq:[[[tickerIdx, weight*1e8, return*1e6], ...]]
};
```

Regenerate it from your own Compustat extract and the page works unchanged.
