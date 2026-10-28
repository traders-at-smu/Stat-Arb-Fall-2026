from types import SimpleNamespace
import numpy as np
import pandas as pd
import pytest
from strategy.preview import (
    validate_orders,
    asset_validation,
    signal_age,
    historical_session_complete,
    latest_complete_session,
    size_orders,
    preclose_readiness,
)


def order(**changes):
    return (
        pd.DataFrame([dict(symbol='SPY', side='BUY', quantity=10) | changes])
    )


@pytest.mark.parametrize('quantity', [0, -1, 1.5, np.nan, np.inf, 'bad', True])
def test_invalid_quantities(quantity):
    with pytest.raises(ValueError):
        validate_orders(order(quantity=quantity))


def test_duplicate_orders():
    with pytest.raises(ValueError):
        validate_orders(pd.concat([order(), order()]))


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_valid_orders(side):
    validate_orders(order(side=side))


def test_unavailable_short_and_portfolio_block():
    result = asset_validation(
        order(side='SELL'),
        lambda _: SimpleNamespace(tradable=True, shortable=False)
    )
    assert not result.eligible.all()
    assert result.reason.iloc[0] == 'NOT SHORTABLE'


def test_api_error_fails_closed():

    def fail(_):
        raise TimeoutError()
    assert not asset_validation(order(), fail).eligible.any()


@pytest.mark.parametrize(
    'date,expected',
    [('2026-10-15', False), ('2026-10-14', True), ('2026-10-20', True)]
)


def test_stale_data(date, expected):
    assert signal_age(date, '2026-10-19', 4)[1] == expected


def auctions():
    return (
        pd.DataFrame([dict(date=d, symbol='SPY', open_auc=100.0,
            close_auc=101.0, factor=1.0) for d in ['2026-10-15',
                '2026-10-16']])
    )


@pytest.mark.parametrize('value', [np.nan, 0, -1, np.inf])
def test_incomplete_or_invalid_historical_auction(value):
    raw = auctions().iloc[:1].copy()
    raw.loc[0, 'close_auc'] = value
    assert not historical_session_complete(raw, ['SPY'])


def test_missing_and_duplicate_auction_symbols():
    raw = auctions().iloc[:1]
    assert not historical_session_complete(raw, ['SPY', 'XLF'])
    assert not historical_session_complete(pd.concat([raw, raw]), ['SPY'])


def test_historical_fallback_retained():
    raw = auctions()
    raw.loc[1, 'close_auc'] = np.nan
    dates = pd.to_datetime(['2026-10-15', '2026-10-16'])
    assert latest_complete_session(
        raw,
        dates,
        pd.Series(True, index=dates),
        ['SPY']
    ) == dates[
        0
    ]


def readiness(**overrides):
    raw = auctions()
    # today's close is not observable or needed
    raw.loc[1, 'close_auc'] = np.nan
    params = dict(
        session='2026-10-16',
        previous_session='2026-10-15',
        signal_date='2026-10-16',
        now='2026-10-16 15:30:00-04:00',
        deadline='2026-10-16 15:40:00-04:00',
        hedge_timing_verified=True,
        inputs_available_at={k: '2026-10-16 10:00:00-04:00' for k in [
            'previous_close', 'current_open', 'model_history', 'adjustments',
                'sizing_prices']}
    )
    params.update(overrides)
    return preclose_readiness(raw, ['SPY'], **params)


def test_preclose_does_not_require_current_close():
    assert readiness() == []


@pytest.mark.parametrize(
    'changes',
    [{'signal_date': '2026-10-15'}, {'now': '2026-10-16 15:40:00-04:00'}, {
        'hedge_timing_verified': False}, {'inputs_available_at': {}}, {
            'inputs_available_at': {
                'sizing_prices': '2026-10-16 16:00:00-04:00'}}]
)


def test_preclose_blocks_stale_late_unverified_inputs(changes):
    assert readiness(**changes)


def test_preclose_missing_open_or_previous_close():
    raw = auctions()
    raw.loc[1, 'open_auc'] = np.nan
    assert preclose_readiness(
        raw,
        ['SPY'],
        session='2026-10-16',
        previous_session='2026-10-15',
        signal_date='2026-10-16',
        now='2026-10-16 15:30-04:00',
        deadline='2026-10-16 15:40-04:00',
        inputs_available_at={}
    )


def test_whole_share_rounding_unchanged():
    result = size_orders(
        pd.Series([0.123, -0.123], index=['SPY', 'XLF']),
        pd.Series([100.0, 100.0], index=['SPY', 'XLF']),
        10000
    )
    assert result.signed_shares.tolist() == [12, -12]


def test_paper_entry_lock_remains_enabled():
    from strategy.paper_trader import ENTRY_EXECUTION_LOCK
    assert ENTRY_EXECUTION_LOCK is True


def test_empty_orders_rejected():
    with pytest.raises(ValueError):
        validate_orders(order().iloc[:0])


def test_missing_previous_close_blocks_readiness():
    raw = auctions()
    raw.loc[0, 'close_auc'] = np.nan
    reasons = preclose_readiness(
        raw,
        ['SPY'],
        session='2026-10-16',
        previous_session='2026-10-15',
        signal_date='2026-10-16',
        now='2026-10-16 15:30-04:00',
        deadline='2026-10-16 15:40-04:00',
        inputs_available_at={}
    )
    assert 'Incomplete close_auc' in reasons
