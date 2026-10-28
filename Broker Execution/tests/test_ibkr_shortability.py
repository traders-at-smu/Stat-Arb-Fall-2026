# Fake TWS callbacks only. No network, authentication or order calls.
from copy import deepcopy
import csv
import json
import os
from types import SimpleNamespace as Obj
import pytest
from ibapi.contract import Contract
from strategy import ibkr_shortability as s


def details(symbol, conid=1, stock_type='ETF', primary='ARCA'):
    c = Contract()
    c.symbol = c.localSymbol = symbol
    c.secType = 'STK'
    c.currency = 'USD'
    c.exchange = 'SMART'
    c.primaryExchange = primary
    c.conId = conid
    return Obj(contract=c, stockType=stock_type, longName=symbol + ' test ETF')


class Fake(s.ShortabilityIB):
    offline_test = True

    def __init__(self, mode='ok'):
        super().__init__()
        self.mode = mode
        self.calls = []
        self.online = False

    def connect(self, host, port, clientId):
        self.calls.append(('connect', host, port, clientId))
        self.online = True
        if self.mode == 'refused':
            self.error(-1, 502, 'Connection refused')
        else:
            self.nextValidId(100)

    def run(self):
        pass

    def isConnected(self):
        return self.online

    def disconnect(self):
        self.calls.append(('disconnect',))
        self.online = False

    def reqContractDetails(self, req, c):
        self.calls.append(('contract', c.symbol))
        if self.mode == 'missing' and c.symbol == 'KRE':
            self.error(req, 200, 'No security definition')
            return
        self.contractDetails(req, details(c.symbol, req))
        if self.mode == 'ambiguous' and c.symbol == 'KRE':
            self.contractDetails(req, details(c.symbol, req + 100))
        self.contractDetailsEnd(req)

    def reqMarketDataType(self, kind):
        self.calls.append(('data_type', kind))

    def reqMktData(self, req, c, generic, snapshot, regulatory, options):
        self.calls.append(
            ('data', req, c.symbol, generic, snapshot, regulatory)
        )
        self.marketDataType(req, 3 if self.mode == 'delayed' else 1)
        if self.mode == 'permission':
            self.error(req, 354, 'Requested market data not subscribed')
            return
        if self.mode == 'empty':
            return
        self.tickGeneric(req, 46, 3.0)
        self.tickSize(req, 89, 1000)

    def cancelMktData(self, req):
        self.calls.append(('unsubscribe_data', req))


def test_resolves_full_universe_priorities_first_and_no_order_paths():
    app = Fake()
    (
        rows,
        meta
    ) = s.scan(
        app,
        contract_timeout=0.05,
        observation_seconds=0.001,
        pace=0
    )
    assert len(rows) == 32 and {r['symbol'] for r in rows} == set(s.SYMBOLS)
    assert [r['symbol'] for r in rows[:6]] == list(s.PRIORITY)
    assert [
        c[1] for c in app.calls if c[0] == 'contract'
    ][
        :6
    ] == list(
        s.PRIORITY
    )
    assert all((r['contract_status'] == 'VERIFIED_US_ETF' for r in rows))
    assert all(
        (r[
            'availability_status'] == \
                'POSITIVE_SHARES_REPORTED_NOT_RESERVED' for r in rows)
    )
    assert all(
        (not r['short_executable'] and (not r[
            'execution_enabled']) for r in rows)
    )
    requests = [c for c in app.calls if c[0] == 'data']
    assert len(
        requests
    ) == 32 and all(
        (c[3:] == ('236', False, False) for c in requests)
    )
    assert len([c for c in app.calls if c[0] == 'unsubscribe_data']) == 32
    assert meta['disconnected'] and meta['worker_stopped']


@pytest.mark.parametrize(
    'mode,status',
    [('delayed', 'DELAYED_DATA_NOT_CURRENT_AVAILABILITY'), ('permission',
        'MARKET_DATA_PERMISSION_MISSING'), ('empty',
            'NO_SHORTABILITY_FIELDS_RECEIVED'), ('refused',
                'CONNECTION_ERROR_DATA_UNVERIFIED')]
)


def test_permissions_delayed_empty_connection_are_distinct(mode, status):
    app = Fake(mode)
    (
        rows,
        meta
    ) = s.scan(
        app,
        contract_timeout=0.02,
        observation_seconds=0.001,
        pace=0
    )
    assert all((r['availability_status'] == status for r in rows))
    assert all(
        (r['borrow_fee_rate'] is None and r[
            'borrow_fee_status'] == 'UNSUPPORTED_TWS_FIELD' for r in rows)
    )
    assert meta['disconnected']


@pytest.mark.parametrize('mode', ['missing', 'ambiguous'])
def test_no_borrow_request_for_unresolved_contract(mode):
    app = Fake(mode)
    (
        rows,
        _
    ) = s.scan(
        app,
        contract_timeout=0.02,
        observation_seconds=0.001,
        pace=0
    )
    row = next((r for r in rows if r['symbol'] == 'KRE'))
    assert row['availability_status'] == 'CONTRACT_UNVERIFIED'
    assert not any((c[0] == 'data' and c[2] == 'KRE' for c in app.calls))


@pytest.mark.parametrize(
    'change',
    [lambda d: setattr(d.contract, 'currency', 'CAD'), lambda d: setattr(
        d.contract, 'primaryExchange', 'SMART'), lambda d: setattr(d.contract,
            'localSymbol', 'WRONG'), lambda d: setattr(d, 'stockType',
                'COMMON')]
)


def test_contract_identity_rejects_wrong_instrument(change):
    d = details('SPY')
    change(d)
    with pytest.raises(ValueError):
        s.choose_contract('SPY', [d])


def test_etf_type_absent_is_not_verified():
    d = details('SPY', stock_type='')
    assert s.choose_contract('SPY', [d]).stockType == ''


@pytest.mark.parametrize(
    'quantity,indicator,datatype,expected',
    [(0, None, 1, 'ZERO_SHARES_REPORTED'), (None, 3, 1,
        'INDICATOR_ONLY_NOT_QUANTITY_CONFIRMATION'), (None, 2, 1,
            'LOCATE_REQUIRED_INDICATOR_ONLY'), (None, 1, 1,
                'UNAVAILABLE_INDICATOR_ONLY'), (100, 1, 1,
                    'CONFLICTING_BORROW_FIELDS'), (0, 3, 1,
                        'CONFLICTING_BORROW_FIELDS'), (100, 3, 2,
                            'FROZEN_DATA_NOT_CURRENT_AVAILABILITY'), (100, 3,
                                None, 'DATA_FRESHNESS_UNVERIFIED')]
)


def test_quantity_and_indicator_are_not_interchangeable(
    quantity,
    indicator,
    datatype,
    expected
):
    row = s.blank('SPY')
    row.update(
        contract_status='VERIFIED_US_ETF',
        shortable_shares=quantity,
        shortability_indicator=indicator,
        market_data_type=datatype
    )
    assert s.availability(row) == expected
    assert row['short_executable'] is False


def test_invalid_and_unrelated_ticks_never_invent_availability():
    app = Fake()
    app.request_map[2000] = 'SPY'
    app.tickSize(2000, 0, 9999)
    app.tickGeneric(2000, 49, 1)
    assert app.rows['SPY']['shortable_shares'] is None
    app.tickSize(2000, 89, -1)
    app.tickGeneric(2000, 46, float('nan'))
    assert app.rows['SPY']['shortable_shares'] is None
    assert app.rows['SPY']['shortability_indicator'] is None
    assert app.rows['SPY']['errors']


def write_alpaca(path, signal='2026-10-15'):
    path.write_text(
        (
            'symbol,tradable,shortable,eligible,side,signal_date\nKRE'
            ',True,False,False,SELL,'
        ) + signal + '\n'
    )


def test_csv_comparison_latest_alpaca_and_honest_dates(tmp_path):
    old = tmp_path / 'validated_orders.csv'
    new = tmp_path / 'dry_run_report_corrected.csv'
    write_alpaca(old)
    write_alpaca(new)
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    app = Fake()
    (
        rows,
        meta
    ) = s.scan(
        app,
        contract_timeout=0.02,
        observation_seconds=0.001,
        pace=0
    )
    output = tmp_path / 'comparison.csv'
    compared = s.write_comparison(
        rows,
        meta,
        output,
        alpaca_directory=tmp_path
    )
    kre = next((r for r in compared if r['symbol'] == 'KRE'))
    assert kre[
        'alpaca_source'
    ] == str(
        new
    ) and kre[
        'alpaca_shortable'
    ] == 'False'
    assert kre[
        'alpaca_signal_date'
    ] == '2026-10-15' and kre[
        'alpaca_observed_at'
    ] == ''
    assert kre[
        'alpaca_file_saved_at'
    ] and 'NOT verified' in kre[
        'alpaca_timestamp_note'
    ]
    assert kre['shares_received_at'] and kre['source_event_at'] == ''
    with output.open() as f:
        saved = list(csv.DictReader(f))
    assert len(saved) == 32 and saved[0]['borrow_fee_rate'] == ''
    assert output.with_suffix('.json').exists()
    with pytest.raises(FileExistsError):
        s.write_comparison(rows, meta, output, alpaca_directory=tmp_path)


def test_malformed_latest_alpaca_snapshot_does_not_silently_fallback(tmp_path):
    good = tmp_path / 'validated_orders.csv'
    bad = tmp_path / 'dry_run_report_corrected.csv'
    write_alpaca(good)
    bad.write_text('symbol\nKRE\n')
    os.utime(good, (1, 1))
    os.utime(bad, (2, 2))
    with pytest.raises(ValueError):
        s.alpaca_snapshot(directory=tmp_path)


def test_no_alpaca_snapshot_is_labelled_not_assumed_available(tmp_path):
    (saved, meta) = s.alpaca_snapshot(directory=tmp_path)
    assert saved == {} and meta['note'] == 'NO_SAVED_ALPACA_SNAPSHOT'


def test_disconnect_on_request_exception():
    app = Fake()

    def fail(*args):
        raise RuntimeError('test request error')
    app.reqContractDetails = fail
    (
        rows,
        meta
    ) = s.scan(
        app,
        contract_timeout=0.02,
        observation_seconds=0.001,
        pace=0
    )
    assert not app.isConnected() and meta['worker_error']
    assert all(
        (r[
            'availability_status'] == 'CONNECTION_ERROR_DATA_UNVERIFIED' \
                for r in rows)
    )


@pytest.mark.parametrize(
    'method',
    ['placeOrder', 'cancelOrder', 'reqGlobalCancel', 'reqAutoOpenOrders']
)


def test_inherited_order_lock_is_preserved(method):
    with pytest.raises(RuntimeError):
        getattr(Fake(), method)()
