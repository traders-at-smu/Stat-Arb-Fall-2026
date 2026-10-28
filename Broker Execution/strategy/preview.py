# Read-only historical preview helpers; no credentials or broker submissions.
import numpy as np
import pandas as pd
from .config import load_settings


def validate_orders(orders):
    required = {'symbol', 'side', 'quantity'}
    if not required.issubset(orders.columns):
        raise ValueError('Missing order columns')
    if (
        orders.empty
        or orders.symbol.isna().any()
        or orders.symbol.duplicated().any()
    ):
        raise ValueError('Empty orders, missing symbol, or duplicate orders')
    if (
        not orders.symbol.map(lambda s: isinstance(s, str) and bool(s.strip(
            ))).all()
    ):
        raise ValueError('Invalid symbol')
    if not orders.side.isin(['BUY', 'SELL']).all():
        raise ValueError('Invalid order side')
    if orders.quantity.map(lambda q: isinstance(q, (bool, np.bool_))).any():
        raise ValueError('Boolean quantities are invalid')
    q = pd.to_numeric(orders.quantity, errors='coerce')
    if not (np.isfinite(q) & (q > 0) & (q % 1 == 0)).all():
        raise ValueError('Invalid whole-share quantities')


def historical_session_complete(records, symbols):
    # Require actual open AND close prints; not a pre-close readiness check.
    if (
        len(records) != len(set(symbols))
        or set(records.symbol) != set(symbols)
    ):
        return False
    values = records[
        ['open_auc', 'close_auc', 'factor']
    ].apply(
        pd.to_numeric,
        errors='coerce'
    )
    return bool((np.isfinite(values) & (values > 0)).to_numpy().all())


def latest_complete_session(raw, dates, ok, symbols):
    sessions = {
        d: g for (d, g) in raw.groupby(pd.to_datetime(raw.date).dt.normalize())
    }
    complete = [
        d for d in dates if bool(ok.loc[d])
        and pd.Timestamp(d).normalize() in sessions
        and historical_session_complete(sessions[pd.Timestamp(d).normalize()],
            symbols)
    ]
    if not complete:
        raise RuntimeError('No complete sessions with valid signals.')
    return complete[-1]


def historical_positions(model, weights, betas, date, universe):
    # Preserve existing preview timing, deliberately distinct from
    # backtest t+1.
    return (
        model.netted(weights.loc[date], betas[0].loc[date], betas[1].loc[date],
            universe)
    )


def next_night_betas(model, overnight_returns, date):
    # Expose the posterior through date using Ian's unchanged filter.
    #
    # Truncate BEFORE calibration: neither later returns nor the missing
    # sentinel
    # enter _noise_scales(). The sentinel only exposes the next
    # random-walk prior.
    # Full-history backtest parity is conditional on identical noise
    # calibration;
    # this as-of replay intentionally calibrates on the supplied prefix
    # instead.
    # Research limitation: unchanged signals built by FT.build_history
    # still use
    # full-sample calibration and revised/fallback price panels. This
    # helper fixes
    # hedge-state observation timing, not the entire pipeline's
    # point-in-time data.
    # No frozen-training or expanding-state calibration policy is
    # introduced.
    if (
        not overnight_returns.index.is_unique
        or not overnight_returns.index.is_monotonic_increasing
    ):
        raise ValueError('Return dates must be unique and sorted')
    date = pd.Timestamp(date)
    if date not in overnight_returns.index:
        raise ValueError('Signal session is missing from overnight returns')
    prefix = overnight_returns.loc[:date].copy()
    # This is a computational sentinel, not a real or assumed trading session.
    sentinel = pd.DataFrame(
        np.nan,
        index=pd.DatetimeIndex([date + pd.Timedelta(days=1)],
            name=prefix.index.name),
        columns=prefix.columns
    )
    (b1, b2) = model.PC.seg_betas(pd.concat([prefix, sentinel]))
    return (b1.iloc[-1], b2.iloc[-1])


def next_night_positions(model, weights, overnight_returns, date, universe):
    (b1, b2) = next_night_betas(model, overnight_returns, date)
    return model.netted(weights.loc[date], b1, b2, universe)


def compare_portfolios(orders, baseline):
    # Compare signed shares, including names rounded to zero on either side.
    columns = ['symbol', 'weight', 'signed_shares']
    if orders.symbol.duplicated().any() or baseline.symbol.duplicated().any():
        raise ValueError('Duplicate comparison symbols')
    result = baseline[
        columns
    ].merge(
        orders[columns],
        on='symbol',
        how='outer',
        suffixes=('_baseline', '_corrected'),
        validate='one_to_one'
    )
    for col in result.columns.drop('symbol'):
        result[col] = result[col].fillna(0)
    result['weight_change'] = result.weight_corrected - result.weight_baseline
    result[
        'signed_share_change'
    ] = result.signed_shares_corrected - result.signed_shares_baseline
    return result


def size_orders(positions, prices, book_size):
    prices = prices.reindex(positions.index)
    if not np.isfinite(book_size) or book_size <= 0:
        raise ValueError('Invalid book size')
    if not np.isfinite(positions.to_numpy()).all():
        raise ValueError('Invalid portfolio weights')
    if not (np.isfinite(prices) & (prices > 0)).all():
        raise ValueError('Missing or invalid prices')
    dollars = positions * book_size
    shares = (
        np.sign(dollars) * np.floor(np.abs(dollars) / prices)
    ).astype(
        int
    )
    orders = pd.DataFrame(
        {'symbol': positions.index, 'weight': positions.values,
            'target_dollars': dollars.values, 'reference_price': prices.values,
                'signed_shares': shares.values}
    )
    orders = orders[orders.signed_shares != 0].copy()
    orders['side'] = np.where(orders.signed_shares > 0, 'BUY', 'SELL')
    orders['quantity'] = orders.signed_shares.abs().astype(int)
    validate_orders(orders)
    return orders


def signal_age(date, today, max_age_days=None):
    if max_age_days is None:
        max_age_days = load_settings()['max_signal_age_days']
    # Legacy calendar-day policy for historical reports only.
    age = (
        pd.Timestamp(today).normalize() - pd.Timestamp(date).normalize()
    ).days
    return (age, age < 0 or age > max_age_days)


def asset_validation(orders, get_asset):
    validate_orders(orders)
    rows = []
    for r in orders.itertuples(index=False):
        tradable = shortable = False
        borrow = 'UNKNOWN'
        try:
            asset = get_asset(r.symbol)
            (
                tradable,
                shortable
            ) = (
                bool(asset.tradable),
                bool(asset.shortable)
            )
            borrow = str(getattr(asset, 'borrow_status', 'UNKNOWN'))
            eligible = tradable and (r.side == 'BUY' or shortable)
            reason = 'NOT TRADABLE' if not tradable else 'NOT SHORTABLE' if \
                r.side == 'SELL' and (
                not shortable
            ) else 'OK'
        except Exception as exc:
            (eligible, reason) = (False, 'API ERROR: ' + type(exc).__name__)
        rows.append(
            dict(symbol=r.symbol, eligible=eligible, tradable=tradable,
                shortable=shortable, borrow_status=borrow, reason=reason)
        )
    return pd.DataFrame(rows)


def preclose_readiness(
    raw,
    symbols,
    *,
    session,
    previous_session,
    now,
    deadline,
    signal_date,
    inputs_available_at,
    hedge_timing_verified=False
):
    # Data/timing prerequisite ONLY; never execution authorization.
    #
    # Caller must supply exchange-calendar sessions/deadline and
    # availability times
    # for EVERY consumed model/sizing input, including adjustment
    # factors/history.
    # Current-session close prints are intentionally not required.
    # Historical reports
    #     cannot supply this provenance and cannot authorize entry.
    reasons = []
    (now, deadline) = (pd.Timestamp(now), pd.Timestamp(deadline))
    (
        session,
        previous_session
    ) = (
        pd.Timestamp(session).normalize(),
        pd.Timestamp(previous_session).normalize()
    )
    if now.tzinfo is None or deadline.tzinfo is None:
        raise ValueError('Timezone-aware now and deadline required')
    if (
        now.tz_convert('America/New_York').date() != session.date()
        or deadline.tz_convert('America/New_York').date() != session.date()
        or now >= deadline
    ):
        reasons.append('Outside intended pre-close session/deadline')
    if (
        previous_session >= session
        or pd.Timestamp(signal_date).normalize() != session
    ):
        reasons.append('Stale or invalid signal/session')
    for (day, col) in [(previous_session, 'close_auc'), (session, 'open_auc')]:
        records = raw[pd.to_datetime(raw.date).dt.normalize() == day]
        vals = pd.to_numeric(records[col], errors='coerce')
        if (
            len(records) != len(set(symbols))
            or set(records.symbol) != set(symbols)
            or (not (np.isfinite(vals) & (vals > 0)).all())
        ):
            reasons.append('Incomplete ' + col)
    required = {
        'previous_close',
        'current_open',
        'model_history',
        'adjustments',
        'sizing_prices'
    }
    if not required.issubset(inputs_available_at):
        reasons.append('Missing input availability provenance')
    for stamp in inputs_available_at.values():
        stamp = pd.Timestamp(stamp)
        if (
            pd.isna(stamp)
            or stamp.tzinfo is None
            or stamp > now
            or (stamp >= deadline)
        ):
            reasons.append('Input unavailable before decision/deadline')
    if not hedge_timing_verified:
        reasons.append('Next-night hedge timing unverified')
    return reasons


def preflight(
    report,
    account,
    assets,
    positions,
    open_orders,
    now,
    limits,
    expected_symbols
):
    # Shared entry checks; returns reasons, NEVER grants execution
    # authorization.
    #
    #     limits: account_id, max_gross, max_symbol, buying_power_buffer (>=1).
    # Overnight sizing uses regt_buying_power, never intraday leveraged
    # buying_power.
    # All broker objects must come from current read-only queries by the
    # caller.
    from strategy.preclose import timestamp, session_date
    failures = []
    try:
        now = timestamp(now)
        session = session_date(report['session'])
        decision = timestamp(report['decision_at'])
        if (
            session.date() != now.date()
            or report['signal_date'] != str(session.date())
        ):
            failures.append('Wrong signal/entry session')
        if (
            not decision <= now < timestamp(report['submission_deadline'])
            or now - decision > pd.Timedelta(seconds=60)
        ):
            failures.append('Stale report or submission deadline reached')
        morning = timestamp(report['morning_asof'])
        if morning.date() != session.date() or not morning <= decision:
            failures.append('Invalid morning snapshot')
        if (
            report.get('mode') != 'PRECLOSE_DRY_RUN'
            or report.get('data_checks_passed') is not True
            or report.get('blockers')
        ):
            failures.append('Pre-close data checks failed')
        if (
            report.get('point_in_time_validated') is not True
            or report.get('research_limitations')
        ):
            failures.append('Point-in-time/research limitations unresolved')
        orders = pd.DataFrame(report['orders'])
        validate_orders(orders)
        targets = pd.Series(report['portfolio_targets'], dtype=float)
        prices = pd.DataFrame(report['sizing_prices'])
        if (
            set(targets.index) != set(expected_symbols)
            or prices.symbol.duplicated().any()
            or set(prices.symbol) != set(expected_symbols)
        ):
            raise ValueError('Incomplete target/price universe')
        for row in prices.to_dict('records'):
            (
                observed,
                received
            ) = (
                timestamp(row['observed_at']),
                timestamp(row['available_at'])
            )
            if (
                observed.date() != session.date()
                or not observed <= received <= decision
                or now - observed > pd.Timedelta(seconds=60)
            ):
                failures.append('Stale/unavailable sizing prices')
                break
        refs = pd.to_numeric(prices.set_index('symbol').price, errors='coerce')
        expected = size_orders(targets, refs, report['book_size'])
        if set(orders.symbol) != set(expected.symbol):
            raise ValueError('Incomplete required positions')
        a = orders.set_index('symbol').sort_index()
        b = expected.set_index('symbol').sort_index()
        if (
            not a.side.equals(b.side)
            or not np.array_equal(a.quantity.to_numpy(), b.quantity.to_numpy())
        ):
            failures.append(
                'Order quantities differ from complete target portfolio'
            )
        for (symbol, row) in a.iterrows():
            asset = assets[symbol]
            status = getattr(asset.status, 'value', asset.status)
            if status != 'active' or not asset.tradable:
                failures.append('Asset unavailable: ' + symbol)
            borrow = getattr(
                getattr(asset, 'borrow_status', None),
                'value',
                getattr(asset, 'borrow_status', None)
            )
            if (
                row.side == 'SELL'
                and (not asset.shortable or not (borrow == 'easy_to_borrow'
                    or (borrow is None and getattr(asset, 'easy_to_borrow',
                        False) is True)))
            ):
                failures.append(
                    'Current borrow unavailable/unverified: ' + symbol
                )
        status = getattr(account.status, 'value', account.status)
        if (
            str(account.id) != str(limits['account_id'])
            or status != 'ACTIVE'
            or any((getattr(account, k, True) for k in ['trading_blocked',
                'account_blocked', 'trade_suspended_by_user']))
        ):
            failures.append('Wrong or blocked paper account')
        if (
            (a.side == 'SELL').any()
            and getattr(account, 'shorting_enabled', False) is not True
        ):
            failures.append('Account shorting is disabled/unverified')
        if positions or open_orders:
            failures.append(
                'Account is not flat or has unresolved open orders'
            )
        gross = float((b.quantity * refs.reindex(b.index)).sum())
        max_symbol = float((b.quantity * refs.reindex(b.index)).max())
        caps = [
            float(limits[k]) for k in ['max_gross', 'max_symbol',
                'buying_power_buffer']
        ]
        if not np.isfinite(caps).all() or min(caps[:2]) <= 0 or caps[2] < 1:
            raise ValueError('Invalid explicit risk limits')
        if gross > caps[0] or max_symbol > caps[1]:
            failures.append('Gross or per-symbol exposure exceeds limit')
        overnight = float(account.regt_buying_power)
        if not np.isfinite(overnight) or gross * caps[2] > overnight:
            failures.append('Insufficient verified overnight buying power')
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        OverflowError
    ) as exc:
        failures.append('Preflight unavailable: ' + str(exc))
    return failures
