#!/usr/bin/env python3
"""
Build SPY / equity-basket / sector-ETF-basket index levels at 10-minute granularity.

Design (mirrors the daily divisor construction exactly):

  For each trading day d, the intraday path is anchored at the PREVIOUS day's
  basket close and moved by the cap-weighted mean of member returns measured
  from the previous day's final 10-minute bucket:

      level(t) = level_close(d-1) * (1 + SUM_i w_i(d-1) * [ p_i(t)/p_i(T_{d-1}) - 1 ])

  where w_i(d-1) is security i's weight in the prior day's total index market
  cap, taken from Compustat (prccd * cshoc, both as reported on that date).

Why weights come from Compustat but returns come from Alpaca:
  - Weights need only the RATIO of caps at a single instant, where the split
    adjustment factor is uniform across the cross-section, so as-reported
    price x as-reported shares is correct.
  - Returns must be split-consistent ACROSS time. Alpaca's adjustment=split
    series is internally consistent, so p(t)/p(t') is a true return even
    across a split date. Mixing an adjusted price with an unadjusted share
    count would understate the cap of any stock that split mid-window, so the
    two sources are deliberately kept to the job each does correctly.

Corporate actions are excluded where the index absorbs them through the divisor
rather than as a price return. The two in this window were found by the >20%
overnight scan that audit_events() re-runs on every build, each cross-checked
against the raw Compustat close so the reason is established rather than assumed:

  HON 2026-06-29  Alpaca shows -46.3% while the raw close moves only -1.9%
                  (232.21 -> 227.80), with cshoc halving and ajexdi 0.5 -> 1.0.
                  The distribution is simply absent from Alpaca's series.
  FDX 2026-06-01  FedEx Freight ex-date. Here BOTH sources show the drop
                  (-20.6% Alpaca / -17.8% raw), so it is a genuine ex-date move.
                  It is still excluded, because FDXF joins the index the same day
                  and counting the parent's drop as a return while adding the
                  child as a member would double-count the separation.

Note the dates: an earlier pass had both events on 2026-06-29, which was wrong
for FDX -- FDXF first traded 2026-06-01. The scan is what caught it.
"""

import csv, collections, datetime as dt, json, math, os, sys, glob

UP = "/mnt/user-data/uploads"
DAILY = f"{UP}/Documents/traders etf strategy/sp500_constituents_daily_2026-03-20_2026-09-18.csv"
ETFD  = f"{UP}/Documents/traders etf strategy/spy_sector_etfs_daily_2026-03-20_2026-09-18.csv"
SECMAP= f"{UP}/Downloads/sp500_sector_membership_map.csv"
OUT   = "/home/claude/out"

SECTOR_ETF = {"10":"XLE","15":"XLB","20":"XLI","25":"XLY","30":"XLP","35":"XLV",
              "40":"XLF","45":"XLK","50":"XLC","55":"XLU","60":"XLRE"}

# Security-days to drop: distribution absorbed by the divisor, not a price return.
SPINOFF_EXCLUDE = {("HON", "2026-06-29"), ("FDX", "2026-06-01")}

# A security's first days of trading are not index returns -- an addition is a
# divisor event. HONA's debut is the clearest case: a single round 200.00
# when-issued print on 2026-06-15, then 269.95 the next day, a spurious +35%.
NEW_ENTRANT_SKIP_DAYS = 2


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- intraday load
def load_intraday(paths):
    """ticker -> {epoch: close}; also returns the sorted global bucket grid.

    Parts overlap (two runs, the first cut short by a browser disconnect), so
    (ticker, epoch) is deduped. Where two runs disagree on the same bucket the
    discrepancy is counted and reported -- it should be zero, and a non-zero
    count means the two pulls are not reproducible and must be investigated
    before anything is built on them.
    """
    px = collections.defaultdict(dict)
    grid = set()
    n = dup = conflict = 0
    worst = 0.0
    for p in paths:
        with open(p) as f:
            r = csv.reader(f)
            next(r)
            for row in r:
                if len(row) < 3 or not row[2]:
                    continue
                t = int(row[1])
                c = float(row[2])
                d = px[row[0]]
                if t in d:
                    dup += 1
                    if d[t] != c:
                        conflict += 1
                        worst = max(worst, abs(d[t] - c) / max(c, 1e-9))
                    continue
                d[t] = c
                grid.add(t)
                n += 1
    log(f"intraday: {n:,} unique rows, {len(px)} tickers, {len(grid):,} buckets "
        f"({dup:,} overlapping rows deduped)")
    if dup:
        log(f"  cross-run check: {conflict:,} disagreements"
            + (f", worst {worst*100:.6f}%" if conflict else " -- runs reproduce exactly"))
    return px, sorted(grid)


# ------------------------------------------------------------------ daily load
def load_daily():
    """date -> {tic: (prccd, cshoc, ajexdi)}  (as reported)."""
    day = collections.defaultdict(dict)
    with open(DAILY) as f:
        for row in csv.DictReader(f):
            try:
                p = float(row["prccd"]); s = float(row["cshoc"])
            except (ValueError, TypeError, KeyError):
                continue
            if p <= 0 or s <= 0:
                continue
            day[row["datadate"]][row["tic"]] = (p, s, row.get("ajexdi") or "1")
    log(f"daily: {len(day)} dates")
    return day


def load_sectors():
    sec = {}
    with open(SECMAP) as f:
        for row in csv.DictReader(f):
            g = (row.get("gsector") or "").strip().split(".")[0]
            if g in SECTOR_ETF:
                sec[row["tic"]] = g
    log(f"sector map: {len(sec)} tickers")
    return sec


def load_etf_daily():
    """date -> {tic: prccd} for SPY + the 11 sector ETFs."""
    out = collections.defaultdict(dict)
    with open(ETFD) as f:
        for row in csv.DictReader(f):
            try:
                out[row["datadate"]][row["tic"]] = float(row["prccd"])
            except (ValueError, TypeError, KeyError):
                continue
    return out


# ------------------------------------------------------------------- utilities
def bucket_days(grid):
    """ordered list of (date_str, [epochs...]) for the intraday grid."""
    byday = collections.defaultdict(list)
    for t in grid:
        byday[dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")].append(t)
    return [(d, sorted(ts)) for d, ts in sorted(byday.items())]


def entrant_skip(px, days):
    """(tic, date) pairs to skip because the security has only just begun trading.

    Returns for a brand-new listing are not index returns: an addition is a
    divisor event. Only securities whose first bar falls AFTER the start of the
    window are affected, so the 500-odd names present on day one are untouched.
    """
    order = [d for d, _ in days]
    pos = {d: i for i, d in enumerate(order)}
    skip = set()
    for tic, series in px.items():
        ds = sorted({dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d") for t in series})
        if not ds or pos.get(ds[0], 0) == 0:
            continue
        for d in ds[:NEW_ENTRANT_SKIP_DAYS]:
            skip.add((tic, d))
    return skip


def audit_events(px, daily, days, out):
    """Re-run the >20% overnight scan that established the exclusions above.

    Kept in the build so that re-running on a different window re-flags its own
    corporate actions instead of inheriting this window's hardcoded two.
    """
    firstb, lastb = collections.defaultdict(dict), collections.defaultdict(dict)
    for tic, series in px.items():
        for t, c in series.items():
            d = dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
            if d not in firstb[tic] or t < firstb[tic][d][0]:
                firstb[tic][d] = (t, c)
            if d not in lastb[tic] or t > lastb[tic][d][0]:
                lastb[tic][d] = (t, c)
    rawclose = collections.defaultdict(dict)
    for d, row in daily.items():
        for tic, (p, s, _) in row.items():
            rawclose[tic][d] = p

    flagged = []
    for tic in firstb:
        ds = sorted(firstb[tic])
        for i in range(1, len(ds)):
            a = lastb[tic][ds[i - 1]][1]
            b = firstb[tic][ds[i]][1]
            if not a or not b:
                continue
            r = b / a - 1.0            # overnight gap: trigger for a closer look
            if abs(r) <= 0.20:
                continue
            # For the VERDICT, compare like with like: Alpaca close-to-close
            # against raw close-to-close. Comparing the overnight gap against a
            # close-to-close move flags any name that simply kept running after
            # the open (MRNA and MRVL both did), which is not a data problem.
            cc = lastb[tic][ds[i]][1] / a - 1.0
            rp = rawclose.get(tic, {}).get(ds[i - 1])
            rn = rawclose.get(tic, {}).get(ds[i])
            rr = (rn / rp - 1.0) if (rp and rn) else None
            handled = ((tic, ds[i]) in SPINOFF_EXCLUDE)
            flagged.append((tic, ds[i], r, rr, handled, cc))

    with open(f"{out}/event_scan.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "date", "overnight_gap", "alpaca_close_to_close",
                    "raw_close_to_close", "excluded", "verdict"])
        for tic, d, r, rr, handled, cc in sorted(flagged, key=lambda x: -abs(x[2])):
            if rr is None:
                v = "no raw comparison"
            elif abs(cc - rr) > 0.10:
                v = "SOURCES DISAGREE - corporate action likely"
            else:
                v = "sources agree - real price move"
            w.writerow([tic, d, f"{r:.4f}", f"{cc:.4f}",
                        "" if rr is None else f"{rr:.4f}", handled, v])

    disagree = [x for x in flagged if x[3] is not None and abs(x[5] - x[3]) > 0.10
                and not x[4]]
    log(f"event scan: {len(flagged)} overnight moves >20%, "
        f"{sum(1 for x in flagged if x[4])} excluded as corporate actions")
    if disagree:
        log("  *** UNHANDLED source disagreements -- review before trusting output:")
        for tic, d, _r, rr, _h, cc in disagree:
            log(f"      {tic} {d}: alpaca close-to-close {cc*100:+.1f}% "
                f"vs raw {rr*100:+.1f}%")
    return flagged


def weights_from_caps(caps):
    tot = sum(caps.values())
    if tot <= 0:
        return {}, 0.0
    return {k: v / tot for k, v in caps.items()}, tot


def main():
    os.makedirs(OUT, exist_ok=True)
    paths = sorted(glob.glob(f"{UP}/Documents/traders etf strategy/intraday/raw/intraday_10min_all.csv"))
    if not paths:
        log("no intraday parts found")
        return 1
    log("parts: " + ", ".join(os.path.basename(p) for p in paths))

    px, grid = load_intraday(paths)
    daily = load_daily()
    sec = load_sectors()
    etfd = load_etf_daily()
    days = bucket_days(grid)
    ddates = sorted(daily)
    last_daily = ddates[-1]

    # -------- per-day cap weights, carried forward past the daily data's end
    def caps_for(date):
        src = date if date in daily else last_daily
        row = daily[src]
        caps, carried = {}, (src != date)
        for tic, (p, s, _) in row.items():
            caps[tic] = p * s
        return caps, carried

    # -------- coverage audit
    full = len(grid)
    cov = {t: len(v) / full for t, v in px.items()}
    thin = sorted((c, t) for t, c in cov.items() if c < 0.95)
    with open(f"{OUT}/coverage.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "bars", "buckets", "coverage"])
        for t in sorted(cov):
            w.writerow([t, len(px[t]), full, f"{cov[t]:.4f}"])
    log(f"coverage: {len(cov)} tickers, {len(thin)} below 95%")

    audit_events(px, daily, days, OUT)
    SKIP = SPINOFF_EXCLUDE | entrant_skip(px, days)
    log(f"excluded security-days: {len(SPINOFF_EXCLUDE)} corporate actions "
        f"+ {len(SKIP)-len(SPINOFF_EXCLUDE)} new-entrant debut days")

    # -------- daily basket closes (recomputed here so the anchor is consistent)
    #          equity basket: chained cap-weighted price returns
    def daily_levels():
        lv_stk, lv_sec = {}, {}
        prev = None
        L_stk = L_sec = None
        for d in ddates:
            if prev is None:
                L_stk = L_sec = etfd.get(d, {}).get("SPY")
                if L_stk is None:
                    prev = d
                    continue
                lv_stk[d] = L_stk
                lv_sec[d] = L_sec
                prev = d
                continue
            pc, _ = caps_for(prev)
            wts, _ = weights_from_caps(pc)
            # equity basket
            num = 0.0
            for tic, w in wts.items():
                if (tic, d) in SKIP:
                    continue
                a = daily[prev].get(tic)
                b = daily[d].get(tic) if d in daily else None
                if not a or not b:
                    continue
                ra = float(a[2] or 1) or 1.0
                rb = float(b[2] or 1) or 1.0
                r = (b[0] / rb) / (a[0] / ra) - 1.0
                num += w * r
            L_stk *= (1.0 + num)
            lv_stk[d] = L_stk
            # sector basket: sector cap weights x sector ETF price returns
            scap = collections.defaultdict(float)
            for tic, c in pc.items():
                g = sec.get(tic)
                if g:
                    scap[g] += c
            stot = sum(scap.values())
            num2 = 0.0
            if stot > 0:
                for g, c in scap.items():
                    e = SECTOR_ETF[g]
                    a = etfd.get(prev, {}).get(e)
                    b = etfd.get(d, {}).get(e)
                    if a and b:
                        num2 += (c / stot) * (b / a - 1.0)
            L_sec *= (1.0 + num2)
            lv_sec[d] = L_sec
            prev = d
        return lv_stk, lv_sec

    lv_stk, lv_sec = daily_levels()
    log(f"daily closes rebuilt: equity {lv_stk[ddates[-1]]:.2f}  sector {lv_sec[ddates[-1]]:.2f}")

    # -------- intraday paths: divisor method, buy-and-hold within the day
    #
    # This is the third construction tried here, and the reasoning matters more
    # than the code:
    #
    #  1. Anchoring each day at the prior OFFICIAL close while measuring the
    #     overnight move from the prior day's last 10-minute bucket mixed two
    #     different prices -- the 15:50 bucket closes before the 16:00 auction --
    #     injecting that residual into every day. Worst day: $6.54.
    #
    #  2. Chaining bucket-to-bucket returns fixed that, but was still wrong in a
    #     subtler way: re-applying fixed weights to every 10-minute return
    #     implicitly REBALANCES the basket back to those weights 39 times a day.
    #     A real index does not. It holds SHARE COUNTS fixed and lets weights
    #     drift with prices, which is buy-and-hold. The two agree only when
    #     dispersion is zero, so the error showed up precisely on high-dispersion
    #     days -- worst was 2026-06-26 at $2.22, the day MSFT ran +5.7% while MU
    #     fell -6.7%, and a day on which SPY's own 10-minute-vs-close drift was
    #     only $0.13, which is what ruled out the auction as the cause.
    #
    #  3. Below: the divisor method proper. Within each day the level is
    #
    #         L(t) = L(base) * SUM_i w_i * p_i(t)/p_i(base)
    #
    #     a weighted sum of PRICE RATIOS, not a weighted sum of returns. Shares
    #     are fixed, weights drift, and the basket is only re-struck once a day
    #     at the prior close -- identical in form to level = SUM(p*s)/divisor.
    #     The base is the prior day's final bucket and the anchor is the intraday
    #     level at that same bucket, so a single price source is used throughout
    #     and the series stays continuous across the overnight gap.
    rows = []
    carried_days = []
    L_stk = L_sec = L_spy = None

    for di, (d, ts) in enumerate(days):
        prev_day = days[di - 1][0] if di > 0 else d
        pc, carried = caps_for(prev_day)
        if carried and d not in carried_days:
            carried_days.append(d)
        wts, _ = weights_from_caps(pc)
        scap = collections.defaultdict(float)
        for tic, c in pc.items():
            g = sec.get(tic)
            if g:
                scap[g] += c
        stot = sum(scap.values()) or 1.0
        sw = {SECTOR_ETF[g]: c / stot for g, c in scap.items()}

        if di == 0:
            # start of the window: strike all three at SPY's first print
            base_t = ts[0]
            L_spy = px.get("SPY", {}).get(base_t)
            L_stk = L_sec = L_spy
            rows.append((base_t, d, L_spy, L_sec, L_stk, 1.0, 1.0))
            buckets = ts[1:]
        else:
            base_t = days[di - 1][1][-1]
            buckets = ts

        # re-strike the basket once, at the base bucket: fixed weights and fixed
        # base prices for the whole day
        A_stk, A_sec, A_spy = L_stk, L_sec, L_spy
        ebase = {tic: px.get(tic, {}).get(base_t) for tic in wts}
        sbase = {e: px.get(e, {}).get(base_t) for e in sw}
        spy_base = px.get("SPY", {}).get(base_t)

        for t in buckets:
            # equity basket: weighted sum of price ratios (buy-and-hold)
            num = wsum = 0.0
            for tic, w in wts.items():
                if (tic, d) in SKIP:
                    continue
                a = ebase.get(tic)
                b = px.get(tic, {}).get(t)
                if a and b:
                    num += w * (b / a)
                    wsum += w
            L_stk = A_stk * (num / wsum) if wsum > 0.30 else L_stk

            # sector basket: same form, sector cap weights x sector ETF ratios
            n2 = w2 = 0.0
            for e, w in sw.items():
                a = sbase.get(e)
                b = px.get(e, {}).get(t)
                if a and b:
                    n2 += w * (b / a)
                    w2 += w
            L_sec = A_sec * (n2 / w2) if w2 > 0.30 else L_sec

            # SPY on the identical scheme, so all three are strictly comparable
            b = px.get("SPY", {}).get(t)
            if spy_base and b:
                L_spy = A_spy * (b / spy_base)

            rows.append((t, d, L_spy, L_sec, L_stk, wsum, w2))

    with open(f"{OUT}/intraday_levels.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["epoch", "date", "spy", "level_sector", "level_equity",
                    "eq_weight_covered", "sec_weight_covered"])
        for r in rows:
            w.writerow([r[0], r[1],
                        "" if r[2] is None else f"{r[2]:.4f}",
                        "" if r[3] is None else f"{r[3]:.4f}",
                        "" if r[4] is None else f"{r[4]:.4f}",
                        f"{r[5]:.4f}", f"{r[6]:.4f}"])
    log(f"wrote {len(rows):,} intraday rows")
    if carried_days:
        log(f"share counts carried forward for: {carried_days[0]} .. {carried_days[-1]} "
            f"({len(carried_days)} days beyond {last_daily})")

    # -------- correctness checks
    #
    # The intraday series is chained from the first 9:30 bucket, while the daily
    # series is chained from a 4:00pm close, so the two sit at different levels
    # by one partial day. Comparing raw levels would just measure that offset.
    # Both are therefore rescaled to a common base -- the final bucket / close of
    # the first shared day -- and compared as cumulative index paths.
    finals = {d: ts[-1] for d, ts in days}
    byts = {r[0]: r for r in rows}

    shared = [d for d, _ in days if d in lv_stk]
    base_d = shared[0]
    bt = finals[base_d]
    k_stk = lv_stk[base_d] / byts[bt][4]
    k_sec = lv_sec[base_d] / byts[bt][3]
    spy_close = {d: etfd.get(d, {}).get("SPY") for d in shared}
    k_spy = spy_close[base_d] / byts[bt][2] if spy_close[base_d] else 1.0

    checks, de, ds, dp = [], [], [], []
    for d in shared:
        r = byts.get(finals[d])
        if not r:
            continue
        ie, isec, isp = r[4] * k_stk, r[3] * k_sec, r[2] * k_spy
        checks.append((d, lv_stk[d], ie, lv_sec[d], isec, spy_close[d], isp))
        de.append(abs(lv_stk[d] - ie))
        ds.append(abs(lv_sec[d] - isec))
        if spy_close[d]:
            dp.append(abs(spy_close[d] - isp))

    log(f"close check over {len(checks)} days (rebased at {base_d}):")
    log(f"  equity : max |diff| ${max(de):.4f}  mean ${sum(de)/len(de):.4f}")
    log(f"  sector : max |diff| ${max(ds):.4f}  mean ${sum(ds)/len(ds):.4f}")
    if dp:
        log(f"  SPY    : max |diff| ${max(dp):.4f}  mean ${sum(dp)/len(dp):.4f}"
            "   <- pure 10-min-bar vs official-close drift, the floor on the others")
    worst = sorted(zip(de, [c[0] for c in checks]), reverse=True)[:5]
    log("  worst equity days: " + ", ".join(f"{d} ${v:.2f}" for v, d in worst))

    with open(f"{OUT}/close_check.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity_daily", "equity_intraday", "sector_daily",
                    "sector_intraday", "spy_close", "spy_intraday"])
        for c in checks:
            w.writerow([c[0]] + [f"{v:.4f}" if v else "" for v in c[1:]])

    # final published levels, on the daily series' scale
    last = checks[-1]
    log(f"final ({last[0]}): SPY ${last[5]:.2f} | equity ${last[1]:.2f} "
        f"| sector ${last[3]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
