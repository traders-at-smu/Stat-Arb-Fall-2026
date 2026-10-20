# Standalone, bounded, read-only TWS diagnostic. No strategy/Alpaca imports.
#
# TWS must have socket API access enabled, the configured socket port, and
# Read-Only
# API checked. This application cannot inspect or change that TWS checkbox. It
# never probes order permissions with an order and never uses client
# 0/autobinding.
# Account identifiers, port numbers and AccountType do not prove
# demo/paper/live.
# An optional --session-label records a user-confirmed TWS label, not API
# proof.
import argparse
from .config import load_settings
import json
import threading
import time
try:
    import ibapi
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
except ImportError:
    ibapi = None
    EClient = EWrapper = object
TAGS = (
    'AccountType,NetLiquidation,TotalCashValue,AvailableFund'
    's,BuyingPower,EquityWithLoanValue,InitMarginReq,MaintMa'
    'rginReq,ExcessLiquidity,Cushion,DayTradesRemaining'
)
HINTS = {
    502: (
        'Cannot connect: check TWS login, socket API enabled, '
        'configured port and localhost permission.'
    ),
    503: (
        'API/TWS version mismatch: use the current official '
        'IBKR Python API distribution.'
    ),
    504: (
        'API is not connected; no account '
        'authentication/permissions established.'
    ),
    326: (
        'Client ID is already in use. Disconnect that '
        'diagnostic client or select another nonzero ID.'
    ),
    321: (
        'Request rejected by TWS. Keep Read-Only enabled; '
        'inspect the returned permission/restriction message.'
    ),
    1100: 'TWS lost brokerage connectivity; results may be incomplete.',
    1300: (
        'TWS socket port changed; rerun using the port '
        'configured in TWS.'
    )
}
INFO_CODES = {2104, 2106, 2107, 2108, 2158}


class State:

    def __init__(self):
        self.ready = threading.Event()
        self.changed = threading.Event()
        self.fatal = threading.Event()
        self.done = {
            k: threading.Event() for k in ['accounts', 'summary', 'positions',
                'open_orders']
        }
        self.lock = threading.RLock()
        self.accounts = []
        self.summary = {}
        self.positions = {}
        self.orders = {}
        self.messages = []
        self.worker_error = None

    def message(self, reqid, code, text):
        with self.lock:
            self.messages.append(
                dict(request_id=reqid, code=code, message=str(text),
                    severity='info' if code in INFO_CODES else \
                        'error_or_restriction', guidance=HINTS.get(code, (
                            'TWS response retained verbatim' \
                        '; missing data is not '
                            'treated as an empty account.'
                        )))
            )
        if code in {502, 503, 504, 326, 1100, 1300}:
            self.fatal.set()
        self.changed.set()
# Keep optional dependency failure actionable while offline tests can import
# State.
if ibapi is not None:

    class ReadOnlyIB(EWrapper, EClient):

        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.state = State()

        def nextValidId(self, orderId):
            # Handshake signal only. Never use the supplied ID to
            # create an order.
            self.state.ready.set()
            self.state.changed.set()

        def managedAccounts(self, accountsList):
            with self.state.lock:
                self.state.accounts = [s for s in accountsList.split(',') if s]
            self.state.done['accounts'].set()
            self.state.changed.set()

        def accountSummary(self, reqId, account, tag, value, currency):
            with self.state.lock:
                self.state.summary.setdefault(
                    account,
                    {}
                )[
                    tag + (':' + currency if currency else '')
                ] = value

        def accountSummaryEnd(self, reqId):
            self.state.done['summary'].set()
            self.state.changed.set()

        def position(self, account, contract, position, avgCost):
            with self.state.lock:
                key = (account, contract.conId)
                self.state.positions[
                    key
                ] = dict(
                    account=account,
                    contract_id=contract.conId,
                    symbol=contract.symbol,
                    security_type=contract.secType,
                    currency=contract.currency,
                    quantity=str(position),
                    average_cost=avgCost
                )

        def positionEnd(self):
            self.state.done['positions'].set()
            self.state.changed.set()

        def openOrder(self, orderId, contract, order, orderState):
            with self.state.lock:
                self.state.orders[
                    order.clientId,
                    orderId
                ] = dict(
                    order_id=orderId,
                    client_id=order.clientId,
                    permanent_id=order.permId,
                    account=order.account,
                    symbol=contract.symbol,
                    action=order.action,
                    quantity=str(order.totalQuantity),
                    order_type=order.orderType,
                    status=orderState.status
                )

        def openOrderEnd(self):
            self.state.done['open_orders'].set()
            self.state.changed.set()

        def connectionClosed(self):
            self.state.changed.set()

        def error(self, *args):
            # Support both the old SDK callback and newer errorTime signatures.
            if len(args) >= 4 and isinstance(args[2], int):
                (reqid, code, text) = (args[0], args[2], args[3])
            elif len(args) >= 3:
                (reqid, code, text) = args[:3]
            else:
                (reqid, code, text) = (-1, -1, str(args))
            self.state.message(reqid, code, text)

        def placeOrder(self, *a, **k):
            raise RuntimeError(
                'Read-only diagnostic: order operations disabled'
            )

        def cancelOrder(self, *a, **k):
            raise RuntimeError(
                'Read-only diagnostic: order operations disabled'
            )

        def reqGlobalCancel(self, *a, **k):
            raise RuntimeError(
                'Read-only diagnostic: order operations disabled'
            )

        def reqAutoOpenOrders(self, *a, **k):
            raise RuntimeError('Read-only diagnostic: binding disabled')

        def reqOpenOrders(self, *a, **k):
            raise RuntimeError('Use nonbinding reqAllOpenOrders only')
else:
    ReadOnlyIB = None


def wait_until(app, predicate, deadline):
    while not predicate():
        remaining = deadline - time.monotonic()
        if (
            remaining <= 0
            or app.state.fatal.is_set()
            or app.state.worker_error
        ):
            return False
        app.state.changed.wait(min(remaining, 0.1))
        app.state.changed.clear()
    return True


def diagnose(
    app,
    host='127.0.0.1',
    port=7497,
    client_id=17,
    timeout=15,
    session_label=None
):
    if (
        host != '127.0.0.1'
        or not 1 <= port <= 65535
        or client_id <= 0
        or (not 0 < timeout <= 60)
    ):
        raise ValueError(
            (
                'Use localhost, valid port, positive nonzero client ID, '
                'and timeout <=60 seconds'
            )
        )
    if session_label not in {None, 'demo', 'paper', 'live'}:
        raise ValueError('Invalid session label')
    deadline = time.monotonic() + timeout
    stop = threading.Event()
    connected = False
    disconnect_error = None

    def worker():
        try:
            app.connect(host, port, clientId=client_id)
            if stop.is_set():
                app.disconnect()
                return
            app.run()
        except Exception as exc:
            app.state.worker_error = type(exc).__name__ + ': ' + str(exc)
            app.state.changed.set()
        finally:
            if stop.is_set():
                try:
                    app.disconnect()
                except Exception:
                    pass
    thread = threading.Thread(
        target=worker,
        daemon=True,
        name='ibkr-readonly-diagnostic'
    )
    try:
        thread.start()
        connected = wait_until(app, app.state.ready.is_set, deadline)
        if connected:
            # All are data queries. No order IDs requested, no
            # binding or edits.
            for (
                name,
                request
            ) in [
                ('accounts', app.reqManagedAccts),
                ('summary', lambda : app.reqAccountSummary(17001, 'All',
                    TAGS)),
                ('positions', app.reqPositions),
                ('open_orders', app.reqAllOpenOrders)
            ]:
                try:
                    request()
                except Exception as exc:
                    app.state.message(
                        -1,
                        -1,
                        name + ' request failed: ' + str(exc)
                    )
            wait_until(
                app,
                lambda : all((e.is_set() for e in app.state.done.values())),
                deadline
            )
    finally:
        stop.set()
        try:
            app.disconnect()
        except Exception as exc:
            disconnect_error = type(exc).__name__
        if thread.ident is not None:
            thread.join(timeout=2)
    with app.state.lock:
        completion = {k: e.is_set() for (k, e) in app.state.done.items()}
        result = dict(
            host=host,
            port=port,
            client_id=client_id,
            api_handshake_succeeded=connected,
            authentication='Existing TWS login accepted AP' \
                'I handshake' if connected else \
                    'Not established; inspect TWS/A' \
                'PI errors',
            environment=dict(classification='unverified',
                user_confirmed_tws_label=session_label, evidence=(
                    'Socket responses do not establish demo/paper/live. '
                    'Port, account prefix and AccountType are not proof.'
                )),
            account_status=(
                'No definitive account trading-status flag requested; '
                'summary values and API access below are diagnostic '
                'only.'
            ),
            account_data_completion=completion,
            incomplete_requests=[k for (k, v) in completion.items() if not v],
            managed_accounts=list(app.state.accounts),
            account_summary={account: dict(tags) for (account,
                tags) in app.state.summary.items()},
            positions=list(app.state.positions.values()),
            open_orders=list(app.state.orders.values()),
            messages=list(app.state.messages),
            worker_error=app.state.worker_error,
            disconnect_error=disconnect_error,
            worker_stopped=not thread.is_alive(),
            disconnected=not app.isConnected(),
            application_read_only=True,
            tws_read_only_setting='Not queryable here; leave Read' \
                '-Only API checked in TWS.',
            sdk_version=getattr(ibapi, '__version__', 'unavailable')
        )
    result[
        'queries_complete'
    ] = connected and all(
        completion.values()
    ) and (
        not result['worker_error']
    )
    result[
        'success'
    ] = result[
        'queries_complete'
    ] and (
        not app.state.fatal.is_set()
    ) and (
        not any((m['severity'] != 'info' for m in result['messages']))
    ) and result[
        'disconnected'
    ] and result[
        'worker_stopped'
    ] and (
        not disconnect_error
    )
    return result


def main():
    settings = load_settings()['ibkr']
    parser = argparse.ArgumentParser(
        description='Read-only IBKR diagnostic; orders disabled'
    )
    parser.add_argument(
        '--host',
        default=settings['host'],
        choices=['127.0.0.1']
    )
    parser.add_argument('--port', type=int, default=settings['port'])
    parser.add_argument('--client-id', type=int, default=settings['client_id'])
    parser.add_argument(
        '--timeout',
        type=float,
        default=settings['timeout_seconds']
    )
    parser.add_argument(
        '--session-label',
        choices=['demo', 'paper', 'live'],
        help='Optional label verified by you in TWS; not API verification'
    )
    args = parser.parse_args()
    if ReadOnlyIB is None:
        parser.exit(
            2,
            (
                'IBKR SDK missing. Install requirements-ibkr.txt into '
                'the project virtual environment.\n'
            )
        )
    try:
        result = diagnose(
            ReadOnlyIB(),
            args.host,
            args.port,
            args.client_id,
            args.timeout,
            args.session_label
        )
    except KeyboardInterrupt:
        print('Interrupted; disconnect requested.')
        return 130
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['success'] else 1
if __name__ == '__main__':
    raise SystemExit(main())
