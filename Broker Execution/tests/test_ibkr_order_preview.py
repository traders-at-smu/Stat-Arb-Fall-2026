# Only preview mappings and simulated fills; no TWS or account access.
import pandas as pd
import pytest
from types import SimpleNamespace as Obj
from strategy import ibkr_order_preview as p


def orders():
    return (
        pd.DataFrame([dict(symbol='SPY', side='BUY', quantity=10,
            signed_shares=10), dict(symbol='XLF', side='SELL', quantity=8,
                signed_shares=-8)])
    )


@pytest.mark.parametrize(
    'phase,kind,tif',
    [('entry', 'MOC', 'DAY'), ('exit', 'MKT', 'OPG')]
)


def test_ibkr_auction_mapping_never_transmits(phase, kind, tif):
    (
        c,
        o
    ) = p.auction_preview(
        'SPY',
        'BUY',
        10,
        Obj(con_id=756733, primary_exchange='ARCA'),
        phase
    )
    assert c.conId == 756733 and c.exchange == 'SMART' and (
        c.secType == 'STK'
    ) and (
        c.currency == 'USD'
    )
    assert o.orderType == kind and o.tif == tif and (o.totalQuantity == 10)
    assert o.transmit is False and o.whatIf is False and (
        o.outsideRth is False
    )


def test_partial_and_zero_fills_determine_exits_without_broker_ledger():
    frame = orders()
    fills = p.simulate(frame, {'SPY': 4, 'XLF': 0})
    contracts = pd.DataFrame(
        [dict(symbol='SPY', con_id=1, primary_exchange='ARCA'), dict(
            symbol='XLF', con_id=2, primary_exchange='ARCA')]
    ).set_index(
        'symbol'
    )
    previews = p.build_previews(frame, contracts, fills)
    exits = previews[previews.phase == 'exit']
    assert len(
        exits
    ) == 1 and exits.iloc[
        0
    ].quantity == 4 and (
        exits.iloc[0].action == 'SELL'
    )
    assert len(previews[previews.phase == 'entry']) == 2
    assert not previews.transmit.any() and (
        not previews.execution_enabled.any()
    )
    assert fills.fill_source.eq('HYPOTHETICAL_FINAL_FILL_SCENARIO').all()


def test_short_fills_reverse_to_buy_covers():
    fills = p.simulate(orders(), {'SPY': 0, 'XLF': 3})
    assert fills.iloc[
        1
    ].exit_side == 'BUY' and fills.iloc[
        1
    ].exit_quantity == 3


@pytest.mark.parametrize('bad', [-1, 11, 1.5, float('nan'), True])
def test_invalid_hypothetical_fills_rejected(bad):
    with pytest.raises(ValueError):
        p.simulate(orders(), {'SPY': bad, 'XLF': 0})


def test_contract_file_requires_full_verified_universe(tmp_path):
    path = tmp_path / 'contracts.csv'
    pd.DataFrame(
        [dict(symbol='SPY', con_id=1, primary_exchange='ARCA',
            stock_type='ETF', contract_status='VERIFIED_US_ETF')]
    ).to_csv(
        path,
        index=False
    )
    with pytest.raises(ValueError):
        p.load_contracts(path)


def test_borrow_comparison_is_unknown_despite_indicators(monkeypatch):
    from strategy.collect_preclose import SYMBOLS
    contracts = pd.DataFrame(
        [dict(symbol=s, con_id=i + 1, contract_status='VERIFIED_US_ETF',
            availability_status='MARKET_DATA_PERMISSION_MISSING',
                scan_finished_at='2026-10-20T17:00:00Z') for (i,
                    s) in enumerate(SYMBOLS)]
    ).set_index(
        'symbol'
    )
    monkeypatch.setattr(
        p,
        'alpaca_snapshot',
        lambda _: ({'XLF': {'shortable': 'False',
            'signal_date': '2026-10-15'}}, dict(
                file_saved_at='2026-10-19T00:00:00Z', source='saved.csv',
                    note='Not observation time'))
    )
    result = p.comparison(orders(), contracts)
    assert len(
        result
    ) == 32 and result.ibkr_borrow_availability.eq(
        'UNKNOWN'
    ).all()
    assert result.ibkr_shortable_shares.eq(
        'UNKNOWN'
    ).all() and result.ibkr_borrow_fee.eq(
        'UNKNOWN'
    ).all()
    assert result.set_index('symbol').loc['XLF', 'alpaca_shortable'] == 'False'
