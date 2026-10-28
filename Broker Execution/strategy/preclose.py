# Offline pre-close snapshot replay. Never creates execution approval or
# orders.
#
# Input JSON: session, morning_asof, history_start, auctions, prices, calendar.
# calendar: coverage_start/end, available_at, source, sessions
# (session/open/close/
# moc_deadline). Sessions must be the provider's COMPLETE schedule in that
# range.
# auctions:
# session/symbol/kind(open|close)/price/factor/observed_at/available_at/
# factor_available_at. These must be archived as-known records, not
# reconstructed
# arrival times attached to today's revised historical files.
# prices: symbol/price/observed_at/available_at. Prices must be fresh
# regular-session
# observations; they are indicative sizing references, never promised MOC
# fills.
#
# Research limitations: Ian calibrates on all supplied returns. Truncating
# the panel
# makes this decision as-of, but earlier residual betas are recalibrated
# using later
# observations within that prefix; this differs from a frozen online state
# ledger.
# Adjustment factors normally derived from today's final daily close are
# unavailable
# in the morning. An independently archived as-of factor source is required.
# Claimed
# availability timestamps cannot establish genuine provenance.
# Calendar/deadline
# routing, adjustment provenance and calibration policy remain unverified,
# hence
# execution_approved and execution_enabled are unconditionally false.
import json
from pathlib import Path
import numpy as np
import pandas as pd
from .preview import next_night_betas, size_orders
from .config import load_settings
LIMITATIONS = [
    (
        'Calendar completeness and account/venue MOC deadlines '
        'require independent verification.'
    ),
    (
        'Auction and adjustment-factor as-of provenance is '
        'supplied, not independently verified.'
    ),
    (
        'Prefix calibration revises earlier residuals; frozen '
        'online calibration remains unverified.'
    ),
    (
        'Supplied history origin/completeness must match model '
        'initialization; this is not independently verified.'
    ),
    (
        'Borrow, account limits, execution reconciliation and '
        'routing are not checked here.'
    )
]


def timestamp(value):
    t = pd.Timestamp(value)
    if pd.isna(t) or t.tzinfo is None:
        raise ValueError('Timezone-aware timestamps required')
    return t.tz_convert('America/New_York')


def session_date(value):
    d = pd.Timestamp(value)
    if pd.isna(d) or d.tzinfo is not None or d != d.normalize():
        raise ValueError('Session dates must be timezone-free dates')
    return d.normalize()


def available(record, observed, arrived, cutoff, earliest=None):
    (obs, arrival) = (timestamp(record[observed]), timestamp(record[arrived]))
    if (
        arrival < obs
        or obs > cutoff
        or arrival > cutoff
        or (earliest is not None and obs < earliest)
    ):
        raise ValueError(
            'Observation missing, stale, or unavailable at decision time'
        )
    return (obs, arrival)


def build_preclose_preview(model, snapshot, now):
    # Fail closed on invalid input; valid input produces an UNAPPROVED preview.
    result = dict(
        mode='PRECLOSE_DRY_RUN',
        execution_approved=False,
        execution_enabled=False,
        data_checks_passed=False,
        point_in_time_validated=False,
        historical_backtest_parity='NOT_CLAIMED',
        blockers=[],
        research_limitations=LIMITATIONS.copy(),
        orders=[]
    )
    try:
        now = timestamp(now)
        day = session_date(snapshot['session'])
        morning = timestamp(snapshot['morning_asof'])
        start = session_date(snapshot['history_start'])
        cal = snapshot['calendar']
        if not cal['source'] or timestamp(cal['available_at']) > morning:
            raise ValueError('Calendar source unavailable by morning')
        if (
            not session_date(cal[
                'coverage_start']) <= start <= day <= session_date(cal[
                    'coverage_end'])
        ):
            raise ValueError('Calendar coverage missing')
        schedule = pd.DataFrame(cal['sessions']).copy()
        schedule['session'] = schedule.session.map(session_date)
        if (
            schedule.session.duplicated().any()
            or not schedule.session.is_monotonic_increasing
        ):
            raise ValueError('Calendar sessions must be unique and sorted')
        schedule = schedule.set_index('session')
        if day not in schedule.index or day.dayofweek >= 5:
            raise ValueError(
                'Market holiday, weekend, or session missing from calendar'
            )
        schedule = schedule.loc[start:day]
        if start not in schedule.index or len(schedule) < 61:
            raise ValueError(
                'Insufficient calendar/model history for 60 completed nights'
            )
        for (d, row) in schedule.iterrows():
            (
                op,
                cl,
                dl
            ) = (
                timestamp(row[k]) for k in ['open', 'close', 'moc_deadline']
            )
            if (
                op.date() != d.date()
                or cl.date() != d.date()
                or (not op < dl < cl)
            ):
                raise ValueError('Invalid calendar times or MOC deadline')
        (
            op,
            cl,
            broker_deadline
        ) = (
            timestamp(schedule.loc[day, k]) for k in ['open', 'close',
                'moc_deadline']
        )
        # Conservative project buffer, including early closes. Not
        # broker certification.
        deadline = min(broker_deadline, cl - pd.Timedelta(minutes=20))
        if now.date() != day.date() or not op <= morning <= now < deadline:
            raise ValueError(
                'Wrong/stale session, morning s' \
                    'napshot, or MOC deadline reach' \
                    'ed'
            )
        if morning > op + pd.Timedelta(hours=2):
            raise ValueError(
                'Morning snapshot must be available within two hours of open'
            )
        symbols = list(model.PC.SYMS)
        raw = pd.DataFrame(snapshot['auctions']).copy()
        required = {
            'session',
            'symbol',
            'kind',
            'price',
            'factor',
            'observed_at',
            'available_at',
            'factor_available_at'
        }
        if not required.issubset(raw.columns):
            raise ValueError(
                'Missing auction fields or availability provenance'
            )
        raw['session'] = raw.session.map(session_date)
        # Refuse future records; do not silently compute with
        # final-close information.
        if (
            (raw.session > day).any()
            or ((raw.session == day) & (raw.kind == 'close')).any()
        ):
            raise ValueError(
                'Future session or current closing auction included'
            )
        if (
            raw.duplicated(['session', 'symbol', 'kind']).any()
            or not raw.kind.isin(['open', 'close']).all()
        ):
            raise ValueError('Duplicate or invalid auction records')
        opens = pd.DataFrame(np.nan, index=schedule.index, columns=symbols)
        (closes, factors) = (opens.copy(), opens.copy())
        for d in schedule.index:
            kinds = [
                'close'
            ] if d == start else [
                'open'
            ] if d == day else [
                'open',
                'close'
            ]
            for kind in kinds:
                rows = raw[(raw.session == d) & (raw.kind == kind)]
                if (
                    len(rows) != len(symbols)
                    or set(rows.symbol) != set(symbols)
                ):
                    raise ValueError(
                        f'Missing auction coverage: {d.date()} {kind}'
                    )
                for r in rows.to_dict('records'):
                    expected = timestamp(schedule.loc[d, kind])
                    (
                        obs,
                        _
                    ) = available(
                        r,
                        'observed_at',
                        'available_at',
                        morning,
                        expected
                    )
                    if obs > expected + pd.Timedelta(minutes=5):
                        raise ValueError(
                            'Auction observation is outside its session window'
                        )
                    if timestamp(r['factor_available_at']) > morning:
                        raise ValueError(
                            'Adjustment factor unavailable by morning'
                        )
                    (price, factor) = (float(r['price']), float(r['factor']))
                    if (
                        not np.isfinite([price, factor]).all()
                        or min(price, factor) <= 0
                    ):
                        raise ValueError(
                            'Invalid auction price or adjustment factor'
                        )
                    if (
                        pd.notna(factors.loc[d, r['symbol']])
                        and factors.loc[d, r['symbol']] != factor
                    ):
                        raise ValueError(
                            'Inconsistent as-of adjustment factors'
                        )
                    factors.loc[d, r['symbol']] = factor
                    (
                        opens if kind == 'open' else closes
                    ).loc[
                        d,
                        r['symbol']
                    ] = price
        returns = opens * factors / (closes * factors).shift(1) - 1
        if not np.isfinite(returns.iloc[-60:].to_numpy()).all():
            raise ValueError('Incomplete last 60 overnight returns')
        prices = pd.DataFrame(snapshot['prices']).copy()
        if (
            len(prices) != len(symbols)
            or prices.symbol.duplicated().any()
            or set(prices.symbol) != set(symbols)
        ):
            raise ValueError('Missing or duplicate sizing prices')
        for r in prices.to_dict('records'):
            (obs, _) = available(r, 'observed_at', 'available_at', now, op)
            if now - obs > pd.Timedelta(seconds=60):
                raise ValueError('Sizing price older than 60 seconds')
        # All observations and factor availability have passed before
        # model calls.
        betas = model.PC.seg_betas(returns)
        residuals = model.PC.resid(
            returns[model.PC.ALL],
            model.PC.spy_panel(returns),
            model.PC.parent_panel(returns),
            *betas
        )
        (weights, ok) = model.weights(residuals, model.PC.ALL)
        if not bool(ok.loc[day]):
            raise ValueError('Invalid morning signal')
        (b1, b2) = next_night_betas(model, returns, day)
        positions = model.netted(weights.loc[day], b1, b2, model.PC.ALL)
        orders = size_orders(
            positions,
            pd.to_numeric(prices.set_index('symbol').price, errors='coerce'),
            snapshot.get('book_size', load_settings()['book_size'])
        )
        price_times = prices.set_index(
            'symbol'
        )[
            ['observed_at', 'available_at']
        ]
        orders = orders.join(price_times, on='symbol')
        orders['execution_approved'] = False
        orders['execution_enabled'] = False
        result.update(
            data_checks_passed=True,
            orders=orders.to_dict('records'),
            session=str(day.date()),
            morning_asof=morning.isoformat(),
            decision_at=now.isoformat(),
            submission_deadline=deadline.isoformat(),
            signal_date=str(day.date()),
            calibration='PREFIX_ONLY_REPLAY',
            history_start=str(start.date()),
            portfolio_targets=positions.to_dict(),
            book_size=snapshot.get('book_size', load_settings()['book_size']),
            sizing_prices=snapshot['prices']
        )
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        OverflowError
    ) as exc:
        result['blockers'].append(str(exc))
    return result


def run_snapshot(model, input_path, output_path, now=None):
    # Offline JSON adapter; no network, credentials, or broker objects.
    snapshot = json.loads(Path(input_path).read_text())
    result = build_preclose_preview(
        model,
        snapshot,
        now
        or pd.Timestamp.now(tz='America/New_York')
    )
    Path(output_path).write_text(json.dumps(result, indent=2, allow_nan=False))
    print('Pre-close data checks:', result['data_checks_passed'])
    print('Execution approved: False; execution enabled: False')
    print('Blockers:', result['blockers'])
    print('Unverified:', result['research_limitations'])
    return result
