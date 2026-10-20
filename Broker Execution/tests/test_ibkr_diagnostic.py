# Offline only: fake API callbacks, no sockets/TWS/account access.
import threading
from types import SimpleNamespace as Obj
import pytest
from strategy import ibkr_diagnostic as d


class Fake:

    def __init__(self, mode='ok'):
        self.state = d.State()
        self.mode = mode
        self.connected = False
        self.calls = []

    def connect(self, host, port, clientId):
        self.calls.append(('connect', host, port, clientId))
        self.connected = True
        if self.mode == 'refused':
            self.state.message(-1, 502, 'Connection refused')
            return
        self.state.ready.set()

    def run(self):
        pass

    def disconnect(self):
        self.calls.append(('disconnect',))
        self.connected = False

    def isConnected(self):
        return self.connected

    def reqManagedAccts(self):
        self.calls.append(('accounts',))
        self.state.accounts = ['TEST_PAPER_ACCOUNT', 'TEST_OTHER_ACCOUNT']
        self.state.done['accounts'].set()

    def reqAccountSummary(self, *args):
        self.calls.append(('summary',))
        if self.mode == 'restricted':
            self.state.message(
                17001,
                321,
                'Demo account request not permitted'
            )
            return
        self.state.summary = {
            'TEST_PAPER_ACCOUNT': {'AccountType': 'INDIVIDUAL',
                'AvailableFunds:USD': '100000'}
        }
        self.state.done['summary'].set()

    def reqPositions(self):
        self.calls.append(('positions',))
        self.state.done['positions'].set()

    def reqAllOpenOrders(self):
        self.calls.append(('open_orders',))
        self.state.done['open_orders'].set()


def test_success_does_not_classify_environment_from_ids_or_port():
    app = Fake()
    result = d.diagnose(app, timeout=0.05)
    assert result['success'] and result['disconnected']
    assert result['environment']['classification'] == 'unverified'
    assert app.calls[0] == ('connect', '127.0.0.1', 7497, 17)
    assert set(
        (c[0] for c in app.calls)
    ) == {
        'connect',
        'accounts',
        'summary',
        'positions',
        'open_orders',
        'disconnect'
    }


def test_user_label_is_not_api_evidence():
    result = d.diagnose(Fake(), timeout=0.05, session_label='demo')
    assert result['environment']['user_confirmed_tws_label'] == 'demo'
    assert result['environment']['classification'] == 'unverified'


@pytest.mark.parametrize('mode', ['restricted', 'refused'])
def test_errors_and_timeouts_disconnect_without_claiming_empty_account(mode):
    app = Fake(mode)
    result = d.diagnose(app, timeout=0.02)
    assert not result['success'] and result['disconnected']
    assert result['messages']
    if mode == 'restricted':
        assert not result['account_data_completion']['summary']
    else:
        assert not result['api_handshake_succeeded']


def test_request_exception_does_not_prevent_other_queries_or_disconnect():
    app = Fake()

    def fail():
        raise RuntimeError('permission denied')
    app.reqPositions = fail
    result = d.diagnose(app, timeout=0.02)
    assert result['disconnected'] and (not result['success'])
    assert result['account_data_completion']['open_orders']


def test_handshake_worker_exception_disconnects():
    app = Fake()

    def fail(*a, **k):
        raise OSError('socket unavailable')
    app.connect = fail
    result = d.diagnose(app, timeout=0.02)
    assert not result[
        'success'
    ] and result[
        'disconnected'
    ] and result[
        'worker_error'
    ]


@pytest.mark.parametrize(
    'kwargs',
    [{'client_id': 0}, {'client_id': -1}, {'port': 0}, {'timeout': 0}, {
        'host': 'remote.invalid'}]
)


def test_invalid_connection_options_never_connect(kwargs):
    app = Fake()
    with pytest.raises(ValueError):
        d.diagnose(app, **kwargs)
    assert not app.calls


def test_sdk_callbacks_retain_state_and_both_error_signatures():
    app = d.ReadOnlyIB()
    app.managedAccounts('TEST_PAPER_ACCOUNT,TEST_OTHER_ACCOUNT,')
    app.nextValidId(500)
    app.accountSummary(
        17001,
        'TEST_PAPER_ACCOUNT',
        'AccountType',
        'INDIVIDUAL',
        ''
    )
    app.accountSummaryEnd(17001)
    contract = Obj(conId=123, symbol='SPY', secType='STK', currency='USD')
    app.position('TEST_PAPER_ACCOUNT', contract, 4, 100.0)
    app.positionEnd()
    order = Obj(
        clientId=9,
        permId=77,
        account='TEST_PAPER_ACCOUNT',
        action='BUY',
        totalQuantity=2,
        orderType='MKT'
    )
    app.openOrder(5, contract, order, Obj(status='Submitted'))
    app.openOrderEnd()
    app.error(-1, 2104, 'Market data farm connected')
    app.error(-1, 1234567890, 326, 'Client ID in use', '')
    assert all((e.is_set() for e in app.state.done.values()))
    assert app.state.positions['TEST_PAPER_ACCOUNT', 123]['quantity'] == '4'
    assert app.state.orders[9, 5]['status'] == 'Submitted'
    assert app.state.messages[0]['severity'] == 'info'
    assert app.state.messages[1]['code'] == 326 and app.state.fatal.is_set()


@pytest.mark.parametrize(
    'method',
    ['placeOrder', 'cancelOrder', 'reqGlobalCancel', 'reqAutoOpenOrders',
        'reqOpenOrders']
)


def test_order_mutation_and_binding_paths_are_disabled(method):
    app = d.ReadOnlyIB()
    with pytest.raises(RuntimeError, match='disabled|nonbinding'):
        getattr(app, method)()


def test_silent_handshake_times_out_and_disconnects():
    app = Fake()

    def silent(*a, **k):
        app.connected = True
    app.connect = silent
    result = d.diagnose(app, timeout=0.02)
    assert not result['api_handshake_succeeded'] and result['disconnected']


def test_permission_error_cannot_become_success_even_with_end_callback():
    app = Fake()
    original = app.reqAccountSummary

    def restricted(*args):
        original(*args)
        app.state.message(17001, 321, 'Read-only/demo restriction')
    app.reqAccountSummary = restricted
    result = d.diagnose(app, timeout=0.02)
    assert result[
        'queries_complete'
    ] and (
        not result['success']
    ) and result[
        'disconnected'
    ]
