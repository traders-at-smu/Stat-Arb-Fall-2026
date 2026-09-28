# Rebuilding SPY's share price from its parts

An interactive study of how closely SPY can be reconstructed from (a) the 500 S&P 500
constituent equities and (b) the eleven Select Sector SPDR ETFs.

Now built on **10-minute bars**: 2,680,494 bars across 528 tickers and 5,109 buckets
(131 sessions x 39 RTH buckets), 20 March to 25 September 2026. The daily series is
retained alongside it — the charts switch between the two, and the 500-equity drill-down
stays daily for size reasons noted below.

Download and open `index.html` — it runs fully offline, no server needed. If GitHub Pages
is enabled for this repo it also serves at
`https://traders-at-smu.github.io/Stat-Arb-Fall-2026/spy-basket-tracking/`.

## Result

On 10-minute bars (20 Mar – 25 Sep 2026, 5,109 buckets):

| Series | End price | Return | Ann. vol | Corr vs SPY | Ann. TE | Max gap |
|---|---|---|---|---|---|---|
| SPY price | $771.30 | +17.79% | 13.20% | 1.0000 | — | — |
| From 500 equities | $770.17 | +17.61% | 13.31% | 0.9968 | 1.06% | $2.44 |
| From sector ETFs | $766.91 | +17.11% | 13.50% | 0.9801 | 2.68% | $11.26 |

On daily closes (20 Mar – 18 Sep 2026, 126 sessions), for comparison:

| Series | End price | Return | Ann. vol | Corr vs SPY | Ann. TE | Max gap |
|---|---|---|---|---|---|---|
| SPY price | $761.69 | +17.44% | 13.49% | 1.0000 | — | — |
| From 500 equities | $761.96 | +17.48% | 13.56% | 0.9936 | 1.53% | $4.84 |
| From sector ETFs | $758.77 | +16.99% | 13.52% | 0.9806 | 2.66% | $12.30 |

**The comparison between those two tables is the point.** Sampling ten times more often
leaves the sector basket's tracking error essentially unchanged (2.66% -> 2.68%), which
is what a *structural* gap looks like; sampling noise would have shrunk. The equity
basket moves the other way and tracks tighter intraday (1.53% -> 1.06%), because the
daily series is sampled at the single noisiest print of the day.

Both baskets are market-cap weighted with a divisor — the way the index itself is
computed. Everything is on a price basis, since SPY's quoted price excludes dividends.

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
| `index.html` | The full interactive page. Self-contained apart from the two data files. |
| `contrib_data.js` | Daily levels, sector weights, per-security weights/returns (1.2 MB). |
| `intraday_data.js` | 10-minute index series + per-bucket sector contributions (643 KB). |
| `build_intraday.py` | The intraday build, including the corporate-action scan. |
| `data/daily_levels.csv` | The three price series, daily, 126 rows. |
| `data/intraday_levels.csv` | The three price series, 10-minute, 5,109 rows. |
| `data/intraday_coverage.csv` | Bars present per ticker out of 5,109 buckets. |
| `data/intraday_close_check.csv` | Intraday series vs the daily build at each close. |
| `data/corporate_action_scan.csv` | Every >20% overnight move, with its verdict. |

The page has five tabs: charts (per-series toggles and a daily/10-minute switch), an
intraday price table with a session picker where each sector figure drills into that
bucket's eleven sector contributions, a daily price table that drills all the way down
to individual securities, sector weights, and the capping analysis.

**Not in this repo:** the raw 10-minute bars (`intraday_10min_all.csv`, 107 MB) exceed
GitHub's 100 MB file limit and are vendor data besides. `build_intraday.py` regenerates
everything here from them.

## Method notes

- **Divisor construction.** Each day's basket return is the prior-day-cap-weighted mean
  of member price returns, chained. Share-count changes (buybacks, ETF creation and
  redemption) are absorbed by the divisor rather than read as price moves — XLB's shares
  rose 23.8% and XLP's fell 13.2% over the window, so this matters.
- **Contribution decomposition.** An index level is total market cap over a divisor, so
  security *i* accounts for `level × weight(i)` dollars of it. Contributions sum to the
  level, not to the day's change.
- **Intraday construction.** Within each session the level is a weighted sum of price
  *ratios* against the prior session's final bucket — shares fixed, weights drifting,
  which is what a divisor index actually does. An earlier version chained bucket-to-bucket
  returns instead; that silently rebalances the basket to fixed weights 39 times a day and
  broke down on high-dispersion sessions. Fixing it cut the worst reconstruction error
  from $6.54 to $2.23.
- **Corporate actions are re-derived, not hardcoded.** `build_intraday.py` scans every
  overnight move above 20% and cross-checks it against the raw vendor close, so a
  different window flags its own events. In this window it finds two: Honeywell's HONA
  distribution on **29 June** (the intraday feed shows −46.3% while the raw close moves
  −1.9%, with share count halving) and FedEx's FDXF spin-off on **1 June**. Both are
  excluded because the index absorbs a distribution through the divisor. The other 18
  large moves are confirmed genuine. New listings also have their first two sessions
  excluded — an index addition is a divisor event, and HONA's debut was a single round
  $200.00 when-issued print followed by a spurious +35%.
- **How well it reproduces the daily build.** At each session's close the equity basket
  lands a mean $0.29 from the daily divisor build and the sector basket $0.22, on a ~$760
  index. The floor is SPY itself: chained through the same bars its final bucket sits a
  mean $0.06 from its official close, because the last bar spans 15:50–16:00 and closes
  *before* the 16:00 auction. Every worst case is 26 June 2026 — the last Friday of June,
  so **Russell reconstitution**, the year's largest closing auction. That day single
  stocks moved hard in the final ten minutes (AAPL +0.87%, AMZN +0.95%) while SPY and
  every sector ETF barely moved. Excluding that one session, the worst equity error falls
  from $2.23 to $0.79. A 10-minute bar cannot see a 16:00 auction print.
- **Intraday coverage.** 528 of 528 tickers return data; most return exactly 5,109 bars,
  the complete grid. The only short names are short for corporate reasons — HONA (2,619
  bars, first traded 15 June), FDXF (3,198, first traded 1 June), and HOLX/CTRA/EA/AVB/EQR
  which stopped trading mid-window. See `data/intraday_coverage.csv`.
- **Share counts** come from the daily vendor extract, which ends 18 September; the last
  four intraday sessions (22–25 September) carry the 18 September counts forward. Weights
  move slowly enough that this is immaterial, but it is an approximation.
- **Membership** is rebuilt every session (504–507 names). Exact 2026 index removal dates
  are not recoverable from the available data, so the thirteen departing names are
  retired against the nine known addition dates; their combined weight is under 0.1%.
- **Known approximation.** The S&P 500 weights by float-adjusted market cap; the source
  supplies total shares outstanding. The resulting top-10 concentration of 38.53% is in
  line with the index's own, so the effect appears minor. The residual 1.53% tracking
  error is concentrated in three sessions (0.90% excluding them) and reverses day to day
  — isolated pricing noise rather than a weighting bias.

## Data source and licensing

Daily data is Compustat Security Daily, accessed through Wharton Research Data Services
(WRDS) under a university subscription. Intraday data is 10-minute bars from the Alpaca
Market Data API (SIP consolidated feed, split-adjusted), pulled on the free tier.

The two are used for different jobs on purpose: **weights** come from Compustat, because
a weight only needs the ratio of market caps at one instant, where the split-adjustment
factor is uniform across the cross-section, so as-reported price x as-reported shares is
right. **Returns** come from Alpaca, because a return has to be split-consistent across
time and that series is internally adjusted. Mixing an adjusted price with an unadjusted
share count would understate the cap of any stock that split mid-window.

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

The intraday layer reads a second file, `intraday_data.js`:

```js
window.INTRA = {
  eps:[...],            // epoch seconds, one per 10-minute bucket
  etfs:[...], names:[...],
  spy:[...], levSec:[...], levStk:[...],
  secContrib:[[...]],   // 11 per bucket; these sum to levSec, not to its change
  meta:{...}
};
```

`build_intraday.py` writes `intraday_levels.csv`, the coverage and close-check tables, and
the corporate-action scan; a companion step turns those into `intraday_data.js`.

### Why the equity drill-down stays daily

Per-bucket contributions for all 500 equities would be roughly 2.5 million entries, far
past what a single self-contained page should carry. The 10-minute layer therefore
embeds the three index series and the eleven sector contributions per bucket; the
security-level detail remains on the daily table. Work needing intraday detail at the
single-name level should go to the raw bars.
