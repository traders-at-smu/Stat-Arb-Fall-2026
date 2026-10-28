# Offline execution-state tests: no credential loading or submit calls.
from copy import deepcopy
import json
from types import SimpleNamespace as Obj
import pandas as pd
import pytest
from strategy.preview import preflight
from strategy import paper_trader as p


@pytest.fixture
def inputs():
    report = dict(
        mode='PRECLOSE_DRY_RUN',
        session='2026-10-19',
        signal_date='2026-10-19',
        decision_at='2026-10-19T15:30:00-04:00',
        morning_asof='2026-10-19T10:00:00-04:00',
        submission_deadline='2026-10-19T15:40:00-04:00',
        data_checks_passed=True,
        blockers=[],
        point_in_time_validated=True,
        research_limitations=[],
        portfolio_targets={'SPY': 0.5, 'XLF': -0.5},
        book_size=10000,
        orders=[dict(symbol='SPY', side='BUY', quantity=50), dict(symbol='XLF',
            side='SELL', quantity=50)],
        sizing_prices=[dict(symbol=s, price=100,
            observed_at='2026-10-19T15:29:45-04:00',
                available_at='2026-10-19T15:29:50-04:00') for s in ['SPY',
                    'XLF']]
    )
    account = Obj(
        id='paper-test',
        status='ACTIVE',
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        regt_buying_power='20000',
        shorting_enabled=True
    )
    assets = {
        s: Obj(status='active', tradable=True, shortable=True,
            borrow_status='easy_to_borrow') for s in ['SPY', 'XLF']
    }
    limits = dict(
        account_id='paper-test',
        max_gross=15000,
        max_symbol=7500,
        buying_power_buffer=1.1
    )
    return (report, account, assets, limits)


def check(
    inputs,
    positions=None,
    open_orders=None,
    now='2026-10-19T15:30:00-04:00'
):
    (r, a, assets, limits) = inputs
    return (
        preflight(r, a, assets, positions or [], open_orders or [], now,
            limits, ['SPY', 'XLF'])
    )


def test_valid_synthetic_preflight_is_not_execution_approval(inputs):
    assert check(inputs) == []
    assert p.ENTRY_EXECUTION_LOCK is True


@pytest.mark.parametrize(
    'problem',
    ['stale', 'session', 'missing_leg', 'duplicate', 'fractional', 'quantity',
        'borrow', 'untradable', 'account', 'gross', 'symbol', 'overnight',
            'buffer', 'unresolved', 'open', 'position', 'prices', 'universe',
                'shorting']
)


def test_shared_preflight_blocks(inputs, problem):
    (r, a, assets, limits) = inputs
    positions = []
    opened = []
    if problem == 'stale':
        r['decision_at'] = '2026-10-19T15:00:00-04:00'
    if problem == 'session':
        r['session'] = '2026-10-16'
    if problem == 'missing_leg':
        r['orders'].pop()
    if problem == 'duplicate':
        r['orders'].append(r['orders'][0].copy())
    if problem == 'fractional':
        r['orders'][0]['quantity'] = 1.5
    if problem == 'quantity':
        r['orders'][0]['quantity'] = 51
    if problem == 'borrow':
        assets['XLF'].borrow_status = 'hard_to_borrow'
    if problem == 'untradable':
        assets['SPY'].tradable = False
    if problem == 'account':
        a.trading_blocked = True
    if problem == 'gross':
        limits['max_gross'] = 9999
    if problem == 'symbol':
        limits['max_symbol'] = 4999
    if problem == 'overnight':
        a.regt_buying_power = '10000'
    if problem == 'buffer':
        limits['buying_power_buffer'] = 0.5
    if problem == 'unresolved':
        r['point_in_time_validated'] = False
    if problem == 'open':
        opened = [Obj()]
    if problem == 'position':
        positions = [Obj()]
    if problem == 'prices':
        r['sizing_prices'][0]['observed_at'] = '2026-10-19T15:00:00-04:00'
    if problem == 'universe':
        r['portfolio_targets'].pop('XLF')
    if problem == 'shorting':
        a.shorting_enabled = False
    assert check(inputs, positions, opened)


def test_report_selection_rejects_historical_and_wrong_session(
    inputs,
    tmp_path
):
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(inputs[0]))
    assert p.load_entry_report(
        path,
        '2026-10-19T15:30:00-04:00'
    )[
        'session'
    ] == '2026-10-19'
    with pytest.raises(ValueError):
        p.load_entry_report(path, '2026-10-20T15:30:00-04:00')
    csv = tmp_path / 'dry_run_report.csv'
    csv.write_text('historical')
    with pytest.raises(ValueError):
        p.load_entry_report(csv, '2026-10-19T15:30:00-04:00')


class NotFound(Exception):
    status_code = 404


class Broker:

    def __init__(self):
        self.orders = {}
        self.positions = []
        self.open = []

    def get_account(self):
        return Obj(id='paper-test')

    def get_order_by_client_id(self, cid):
        if cid not in self.orders:
            raise NotFound()
        result = self.orders[cid]
        if isinstance(result, Exception):
            raise result
        return result

    def get_all_positions(self):
        return self.positions

    def get_orders(self, **kwargs):
        return self.open

    def get_calendar(self, filters=None):
        assert filters is not None and filters.start < filters.end
        return [Obj(date='2026-10-19'), Obj(date='2026-10-20')]

    def submit_order(self, *a, **k):
        pytest.fail('No order submission permitted')


def prepare(path):
    orders = pd.DataFrame([dict(symbol='SPY', side='BUY', quantity=10)])
    return (
        p.reserve_orders(path, 'paper-test', '2026-10-19', 'entry', orders)[0]
    )


def status(cid, filled=10, state='filled'):
    return (
        Obj(id='broker-1', client_order_id=cid, symbol='SPY', side='buy',
            qty='10', filled_qty=str(filled),
                filled_avg_price='100' if filled else None, status=state)
    )


def test_restart_duplicate_preparation_is_refused(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    assert cid == p.client_order_id('paper-test', '2026-10-19', 'entry', 'SPY')
    with pytest.raises(RuntimeError):
        prepare(path)
    ledger = json.loads(path.read_text())
    assert ledger['orders'][cid]['requested_qty'] == 10
    assert ledger['orders'][cid]['submission_state'] == 'prepared'
    assert not p.reconcile(Broker(), path)['blockers']


def test_fills_persist_and_exits_use_only_actual_strategy_quantity(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.orders[cid] = status(cid, 4, 'canceled')
    broker.positions = [Obj(symbol='SPY', qty='4')]
    state = p.reconcile(broker, path)
    assert not state['blockers'] and state['events']
    row = json.loads(path.read_text())['orders'][cid]
    assert row[
        'filled_qty'
    ] == 4 and row[
        'broker_order_id'
    ] == 'broker-1' and (
        row['filled_avg_price'] == 100
    )
    exits = p.exit_requests(broker, path, '2026-10-20', '2026-10-19')
    assert len(
        exits
    ) == 1 and exits[
        0
    ].qty == 4 and (
        exits[0].side.value == 'sell'
    )
    ids = p.reserve_orders(
        path,
        'paper-test',
        '2026-10-20',
        'exit',
        pd.DataFrame([dict(symbol='SPY', side='SELL', quantity=4)]),
        source_session='2026-10-19'
    )
    with pytest.raises(RuntimeError):
        p.exit_requests(broker, path, '2026-10-20', '2026-10-19')
    exit_order = Obj(
        id='broker-exit',
        client_order_id=ids[0],
        symbol='SPY',
        side='sell',
        qty='4',
        filled_qty='4',
        filled_avg_price='101',
        status='filled'
    )
    broker.orders[ids[0]] = exit_order
    broker.positions = []
    assert not p.reconcile(broker, path)['blockers']


def test_active_partial_fill_blocks_exits_and_auto_retry(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.orders[cid] = status(cid, 4, 'partially_filled')
    broker.positions = [Obj(symbol='SPY', qty='4')]
    state = p.reconcile(broker, path)
    assert state['blockers'] and state['automatic_retry_allowed'] is False
    with pytest.raises(RuntimeError):
        p.exit_requests(broker, path, '2026-10-20', '2026-10-19')


def test_rejection_is_recorded_without_retry(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.orders[cid] = status(cid, 0, 'rejected')
    result = p.reconcile(broker, path)
    assert result['events'] and (not result['blockers'])
    assert p.exit_requests(broker, path, '2026-10-20', '2026-10-19') == []
    with pytest.raises(RuntimeError):
        prepare(path)


@pytest.mark.parametrize(
    'problem',
    ['uncertain', 'discrepancy', 'unrelated', 'identity', 'regression',
        'query_error', 'wrong_account', 'foreign_order']
)


def test_reconciliation_refuses_ambiguous_state(tmp_path, problem):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.orders[cid] = status(cid)
    broker.positions = [Obj(symbol='SPY', qty='10')]
    if problem == 'uncertain':
        p.mark_submission_uncertain(path, 'paper-test', cid)
        broker.orders = {}
        broker.positions = []
    if problem == 'discrepancy':
        broker.positions = [Obj(symbol='SPY', qty='9')]
    if problem == 'unrelated':
        broker.positions.append(Obj(symbol='XLF', qty='5'))
    if problem == 'identity':
        broker.orders[cid].symbol = 'XLF'
    if problem == 'regression':
        p.reconcile(broker, path)
        broker.orders[cid] = status(cid, 5, 'canceled')
    if problem == 'query_error':
        broker.orders[cid] = TimeoutError()
    if problem == 'foreign_order':
        broker.open = [Obj(id='untracked')]
    if problem == 'wrong_account':
        broker.get_account = lambda : Obj(id='other')
        with pytest.raises(ValueError):
            p.reconcile(broker, path)
        return
    assert p.reconcile(broker, path)['blockers']
    with pytest.raises(RuntimeError):
        p.exit_requests(broker, path, '2026-10-20', '2026-10-19')


def test_both_submission_commands_locked_before_broker_access(monkeypatch):

    def forbidden():
        pytest.fail('Locked command must not access broker/credentials')
    monkeypatch.setattr(p, 'broker', forbidden)
    for command in ['submit-entry', 'submit-exit']:
        monkeypatch.setattr('sys.argv', ['paper_trader', command])
        with pytest.raises(RuntimeError, match='LOCKED'):
            p.main()


def test_short_exit_covers_only_reconciled_short(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = p.reserve_orders(
        path,
        'paper-test',
        '2026-10-19',
        'entry',
        pd.DataFrame([dict(symbol='SPY', side='SELL', quantity=10)])
    )[
        0
    ]
    broker = Broker()
    r = status(cid, 3, 'expired')
    r.side = 'sell'
    broker.orders[cid] = r
    broker.positions = [Obj(symbol='SPY', qty='-3')]
    requests = p.exit_requests(broker, path, '2026-10-20', '2026-10-19')
    assert requests[0].side.value == 'buy' and requests[0].qty == 3


def test_accepted_order_without_fill_is_persisted_and_blocks_exits(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.orders[cid] = status(cid, 0, 'accepted')
    assert p.reconcile(broker, path)['blockers']
    saved = json.loads(path.read_text())['orders'][cid]
    assert saved['status'] == 'accepted' and saved['filled_qty'] == 0
    assert saved['broker_order_id'] == 'broker-1'


def test_completed_old_cycle_does_not_block_next_cycle_same_symbol(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.orders[cid] = status(cid)
    exitid = p.reserve_orders(
        path,
        'paper-test',
        '2026-10-20',
        'exit',
        pd.DataFrame([dict(symbol='SPY', side='SELL', quantity=10)]),
        source_session='2026-10-19'
    )[
        0
    ]
    out = status(exitid)
    out.id = 'exit-1'
    out.side = 'sell'
    broker.orders[exitid] = out
    newer = p.reserve_orders(
        path,
        'paper-test',
        '2026-10-20',
        'entry',
        pd.DataFrame([dict(symbol='SPY', side='BUY', quantity=10)])
    )[
        0
    ]
    new = status(newer)
    new.id = 'entry-2'
    broker.orders[newer] = new
    broker.positions = [Obj(symbol='SPY', qty='10')]
    broker.get_calendar = lambda **kwargs: [
        Obj(date='2026-10-20'),
        Obj(date='2026-10-21')
    ]
    requests = p.exit_requests(broker, path, '2026-10-21', '2026-10-20')
    assert len(requests) == 1 and requests[0].qty == 10


def test_holiday_or_wrong_next_session_blocks_exits(tmp_path):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    broker.get_calendar = lambda **kwargs: [Obj(date='2026-10-19')]
    with pytest.raises(ValueError, match='next calendar'):
        p.exit_requests(broker, path, '2026-10-20', '2026-10-19')


def test_corrupt_ledger_is_not_replaced(tmp_path):
    path = tmp_path / 'ledger.json'
    path.write_text('{invalid')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        p.reconcile(Broker(), path)
    assert path.read_bytes() == before


def test_uncertain_attempt_is_recovered_by_client_id_without_resubmission(
    tmp_path
):
    path = tmp_path / 'ledger.json'
    cid = prepare(path)
    broker = Broker()
    p.mark_submission_uncertain(path, 'paper-test', cid)
    with pytest.raises(RuntimeError):
        p.mark_submission_uncertain(path, 'paper-test', cid)
    broker.orders[cid] = status(cid)
    broker.positions = [Obj(symbol='SPY', qty='10')]
    result = p.reconcile(broker, path)
    assert not result['blockers'] and (not result['automatic_retry_allowed'])
    assert json.loads(
        path.read_text()
    )[
        'orders'
    ][
        cid
    ][
        'submission_state'
    ] == 'broker_observed'
