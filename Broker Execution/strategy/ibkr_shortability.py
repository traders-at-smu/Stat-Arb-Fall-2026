# Read-only ETF contracts + indicative borrow comparison; no trading approval.
#
# Uses reqContractDetails, reqMarketDataType, streaming reqMktData(generic
# 236),
# and cancelMktData ONLY for subscriptions created here. No order operations.
# Tick 46 is a shortability INDICATOR, tick 89 a reported share count. Neither
# reserves shares or proves account-level executability. Local callback receipt
# is recorded, not invented exchange observation time. Market data type must be
# reported by TWS; requesting live/delayed alone does not establish freshness.
#
# The documented TWS generic ticks used here have no indicative borrow-fee
# field.
# Fees remain blank/UNSUPPORTED_TWS_FIELD; verify separately in IBKR's SLB
# tool.
# Source: https://www.interactivebrokers.com/docs/tws-api/doc/md/streaming-data
import argparse
from .config import load_settings
from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
from .ibkr_diagnostic import ReadOnlyIB, wait_until
from .collect_preclose import SYMBOLS
ROOT = Path(__file__).resolve().parents[1]
PRIORITY = ('KRE', 'IAI', 'IHI', 'XPH', 'JETS', 'IYT')
ORDER = PRIORITY + tuple((s for s in SYMBOLS if s not in PRIORITY))
PERMISSIONS = {354, 10089, 10090, 10167, 10168, 10186}
US_PRIMARY = {'ARCA', 'NASDAQ', 'NYSE', 'AMEX', 'BATS', 'ISLAND', 'NYSEARCA'}


def utc():
    return datetime.now(timezone.utc).isoformat()


def blank(symbol):
    return dict(
        symbol=symbol,
        priority=symbol in PRIORITY,
        contract_status='NOT_REQUESTED',
        con_id=None,
        primary_exchange='',
        long_name='',
        stock_type='',
        contract_received_at='',
        market_data_type=None,
        shortability_indicator=None,
        indicator_received_at='',
        shortable_shares=None,
        shares_received_at='',
        borrow_fee_rate=None,
        borrow_fee_status='UNSUPPORTED_TWS_FIELD',
        source_event_at='',
        timestamp_basis=(
            'LOCAL_CALLBACK_RECEIPT; source event time not supplied'
        ),
        errors=[],
        short_executable=False,
        execution_enabled=False,
    )


def choose_contract(symbol, details):
    candidates = {}
    for d in details:
        c = d.contract
        if (
            c.symbol == symbol
            and c.localSymbol == symbol
            and (c.secType == 'STK')
            and (c.currency == 'USD')
            and (c.primaryExchange in US_PRIMARY)
            and (c.conId > 0)
        ):
            candidates.setdefault(c.conId, []).append(d)
    if len(candidates) != 1:
        raise ValueError('MISSING_OR_AMBIGUOUS_US_CONTRACT')
    group = next(iter(candidates.values()))
    if (
        len({(x.contract.primaryExchange, getattr(x, 'stockType',
            '')) for x in group}) != 1
    ):
        raise ValueError('CONFLICTING_CONTRACT_DETAILS')
    d = group[0]
    if getattr(d, 'stockType', '') not in {'', 'ETF'}:
        raise ValueError('NON_ETF_STOCK_TYPE')
    return d


def availability(row):
    # Classifies provider observations, never a locate or an execution
    # approval.
    if (
        row['contract_status'] not in {'VERIFIED_US_ETF',
            'RESOLVED_ETF_TYPE_UNVERIFIED'}
    ):
        return 'CONTRACT_UNVERIFIED'
    if any((e['code'] in PERMISSIONS for e in row['errors'])):
        return 'MARKET_DATA_PERMISSION_MISSING'
    if row['errors']:
        return 'REQUEST_ERROR_OR_RESTRICTION'
    if row['market_data_type'] in {3, 4}:
        return 'DELAYED_DATA_NOT_CURRENT_AVAILABILITY'
    if row['market_data_type'] == 2:
        return 'FROZEN_DATA_NOT_CURRENT_AVAILABILITY'
    if row['market_data_type'] != 1:
        return 'DATA_FRESHNESS_UNVERIFIED'
    (q, v) = (row['shortable_shares'], row['shortability_indicator'])
    if q is not None and q > 0 and (v is not None) and (v <= 1.5):
        return 'CONFLICTING_BORROW_FIELDS'
    if q == 0 and v is not None and (v > 2.5):
        return 'CONFLICTING_BORROW_FIELDS'
    if q is not None:
        return (
            'POSITIVE_SHARES_REPORTED_NOT_RESERVED' if q > 0 else \
                'ZERO_SHARES_REPORTED'
        )
    if v is not None:
        if v > 2.5:
            return 'INDICATOR_ONLY_NOT_QUANTITY_CONFIRMATION'
        if v > 1.5:
            return 'LOCATE_REQUIRED_INDICATOR_ONLY'
        return 'UNAVAILABLE_INDICATOR_ONLY'
    return 'NO_SHORTABILITY_FIELDS_RECEIVED'
if ReadOnlyIB is not None:

    class ShortabilityIB(ReadOnlyIB):

        def __init__(self):
            super().__init__()
            self.rows = {s: blank(s) for s in ORDER}
            self.details = {}
            self.request_map = {}
            self.contract_done = set()

        def contractDetails(self, reqId, contractDetails):
            with self.state.lock:
                if reqId in self.request_map:
                    self.details.setdefault(reqId, []).append(contractDetails)
                    self.rows[
                        self.request_map[reqId]
                    ][
                        'contract_received_at'
                    ] = utc()

        def contractDetailsEnd(self, reqId):
            with self.state.lock:
                self.contract_done.add(reqId)
            self.state.changed.set()

        def marketDataType(self, reqId, marketDataType):
            with self.state.lock:
                if reqId in self.request_map:
                    self.rows[
                        self.request_map[reqId]
                    ][
                        'market_data_type'
                    ] = marketDataType
            self.state.changed.set()

        def tickGeneric(self, reqId, tickType, value):
            if tickType != 46:
                return
            with self.state.lock:
                if reqId not in self.request_map:
                    return
                row = self.rows[self.request_map[reqId]]
                if math.isfinite(float(value)) and 0 <= float(value) <= 3:
                    row.update(
                        shortability_indicator=float(value),
                        indicator_received_at=utc()
                    )
                else:
                    row.update(
                        shortability_indicator=None,
                        indicator_received_at=utc()
                    )
                    row[
                        'errors'
                    ].append(
                        dict(code=-1, message='Invalid shortability indicator')
                    )
            self.state.changed.set()

        def tickSize(self, reqId, tickType, size):
            if tickType != 89:
                return
            with self.state.lock:
                if reqId not in self.request_map:
                    return
                row = self.rows[self.request_map[reqId]]
                q = float(size)
                if (
                    math.isfinite(q)
                    and q >= 0
                    and q.is_integer()
                    and (q < 1000000000000.0)
                ):
                    row.update(
                        shortable_shares=int(q),
                        shares_received_at=utc()
                    )
                else:
                    row.update(shortable_shares=None, shares_received_at=utc())
                    row[
                        'errors'
                    ].append(
                        dict(code=-1,
                            message='Shortable-share field missing/' \
                        'invalid sentinel')
                    )
            self.state.changed.set()

        def error(self, *args):
            super().error(*args)
            if len(args) >= 4 and isinstance(args[2], int):
                (req, code, text) = (args[0], args[2], args[3])
            elif len(args) >= 3:
                (req, code, text) = args[:3]
            else:
                return
            with self.state.lock:
                if req in self.request_map:
                    self.rows[
                        self.request_map[req]
                    ][
                        'errors'
                    ].append(
                        dict(code=code, message=str(text))
                    )
                    if 1000 <= req < 2000:
                        self.contract_done.add(req)
            self.state.changed.set()
else:
    ShortabilityIB = None


def scan(
    app,
    port=7497,
    client_id=17,
    contract_timeout=20,
    observation_seconds=10,
    pace=0.06
):
    if (
        not 1 <= port <= 65535
        or client_id <= 0
        or (not 0 < contract_timeout <= 60)
        or (not 0 < observation_seconds <= 60)
        or (pace < 0)
    ):
        raise ValueError('Invalid port/client ID/timeouts')
    if pace < 0.05 and (not getattr(app, 'offline_test', False)):
        raise ValueError('Live request pacing must be >=0.05 seconds')
    stop = threading.Event()
    subscriptions = []
    handshake = False
    disconnect_error = None

    def worker():
        try:
            app.connect('127.0.0.1', port, clientId=client_id)
            if not stop.is_set():
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
        name='ibkr-shortability-readonly'
    )
    started = utc()
    try:
        thread.start()
        handshake = wait_until(
            app,
            app.state.ready.is_set,
            time.monotonic() + contract_timeout
        )
        if handshake:
            from ibapi.contract import Contract
            requested = []
            for (i, s) in enumerate(ORDER):
                if app.state.fatal.is_set() or app.state.worker_error:
                    break
                req = 1000 + i
                with app.state.lock:
                    app.request_map[req] = s
                    app.rows[s]['contract_status'] = 'REQUESTED'
                c = Contract()
                c.symbol = s
                c.secType = 'STK'
                c.exchange = 'SMART'
                c.currency = 'USD'
                app.reqContractDetails(req, c)
                requested.append(req)
                if pace:
                    stop.wait(pace)
            wait_until(
                app,
                lambda : all((r in app.contract_done for r in requested)),
                time.monotonic() + contract_timeout
            )
            # live when entitled, delayed fallback otherwise;
            # callback is authoritative
            app.reqMarketDataType(3)
            for (i, s) in enumerate(ORDER):
                if app.state.fatal.is_set() or app.state.worker_error:
                    break
                req = 1000 + i
                try:
                    with app.state.lock:
                        if req not in app.contract_done:
                            raise ValueError('CONTRACT_DETAILS_TIMEOUT')
                        d = choose_contract(s, app.details.get(req, []))
                        c = deepcopy(d.contract)
                        app.rows[
                            s
                        ].update(
                            contract_status='VERIFIED_US_ETF' if getattr(d,
                                'stockType',
                                    '') == 'ETF' else \
                        'RESOLVED_ETF_TYPE_UNVERIFIED',
                            con_id=c.conId,
                            primary_exchange=c.primaryExchange,
                            stock_type=getattr(d, 'stockType', ''),
                            long_name=getattr(d, 'longName', '')
                        )
                        app.request_map[2000 + i] = s
                    c.exchange = 'SMART'
                    subscriptions.append(2000 + i)
                    app.reqMktData(2000 + i, c, '236', False, False, [])
                except ValueError as exc:
                    with app.state.lock:
                        app.rows[s]['contract_status'] = str(exc)
                if pace:
                    stop.wait(pace)
            # Allow generic indicators and quantities to arrive independently.
            end = time.monotonic() + observation_seconds
            while (
                time.monotonic() < end
                and (not app.state.fatal.is_set())
                and (not app.state.worker_error)
            ):
                stop.wait(min(0.1, max(0, end - time.monotonic())))
    except Exception as exc:
        app.state.worker_error = type(exc).__name__ + ': ' + str(exc)
    finally:
        for req in subscriptions:
            # unsubscribe this diagnostic's stream; NEVER cancelOrder
            try:
                app.cancelMktData(req)
            except Exception:
                pass
        stop.set()
        try:
            app.disconnect()
        except Exception as exc:
            disconnect_error = type(exc).__name__
        if thread.ident is not None:
            thread.join(timeout=2)
    with app.state.lock:
        rows = deepcopy(list(app.rows.values()))
        messages = deepcopy(app.state.messages)
    for row in rows:
        row['availability_status'] = availability(row)
        row['scan_started_at'] = started
        row['scan_finished_at'] = utc()
        row['account_environment'] = 'UNVERIFIED_TWS_SESSION'
        if (
            app.state.fatal.is_set()
            or app.state.worker_error
            or thread.is_alive()
            or app.isConnected()
            or disconnect_error
        ):
            row['availability_status'] = 'CONNECTION_ERROR_DATA_UNVERIFIED'
        elif (
            any((m['request_id'] == -1 and m[
                'code'] in PERMISSIONS for m in messages))
        ):
            row['availability_status'] = 'MARKET_DATA_PERMISSION_MISSING'
    return (
        (rows, dict(api_handshake_succeeded=handshake,
            disconnected=not app.isConnected(),
                worker_stopped=not thread.is_alive(),
                    worker_error=app.state.worker_error,
                        disconnect_error=disconnect_error, messages=messages,
                            application_read_only=True,
                                execution_enabled=False, note=(
                    'Reported inventory is indicative, not reserved; '
                    'account permissions/locates/fees/execution remain '
                    'unverified.'
                )))
    )


def alpaca_snapshot(path=None, directory=ROOT / 'data'):
    if path is None:
        candidates = [
            Path(directory) / n for n in ['dry_run_report_corrected.csv',
                'dry_run_report.csv', 'validated_orders.csv']
        ]
        candidates = [p for p in candidates if p.exists()]
        if not candidates:
            return (
                ({}, dict(source='', file_saved_at='',
                    note='NO_SAVED_ALPACA_SNAPSHOT'))
            )
        path = max(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))
    path = Path(path)
    with path.open(newline='') as f:
        records = list(csv.DictReader(f))
    if (
        not records
        or any((not {'symbol', 'shortable', 'tradable'}.issubset(
            r) for r in records))
    ):
        raise ValueError(
            (
                'Latest Alpaca snapshot missing eligibility fields; no '
                'silent fallback'
            )
        )
    if len({r['symbol'] for r in records}) != len(records):
        raise ValueError('Duplicate Alpaca snapshot symbols')
    return (
        ({r['symbol']: r for r in records}, dict(source=str(path),
            file_saved_at=datetime.fromtimestamp(path.stat().st_mtime,
                tz=timezone.utc).isoformat(), note=(
                    'File-save time is NOT verified eligibility observation '
                    'time; signal_date is separate.'
                )))
    )


def write_comparison(
    rows,
    metadata,
    output,
    alpaca_path=None,
    alpaca_directory=ROOT / 'data'
):
    (saved, provenance) = alpaca_snapshot(alpaca_path, alpaca_directory)
    comparison = []
    for row in rows:
        other = saved.get(row['symbol'], {})
        r = dict(row)
        r['errors'] = json.dumps(r['errors'])
        r.update(
            alpaca_source=provenance['source'],
            alpaca_file_saved_at=provenance['file_saved_at'],
            alpaca_observed_at=other.get('observed_at', ''),
            alpaca_signal_date=other.get('signal_date', ''),
            alpaca_timestamp_note=provenance['note'],
            alpaca_shortable=other.get('shortable', 'UNKNOWN'),
            alpaca_tradable=other.get('tradable', 'UNKNOWN'),
            alpaca_side=other.get('side', ''),
            alpaca_order_eligible=other.get('eligible', other.get('valid',
                'UNKNOWN')),
            alpaca_borrow_status=other.get('borrow_status', 'UNKNOWN')
        )
        comparison.append(r)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sidecar = output.with_suffix('.json')
    # Refuse overwrites, preserving every previous diagnostic/report.
    if output.exists() or sidecar.exists():
        raise FileExistsError('Comparison output already exists')
    with output.open('x', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)
    with sidecar.open('x') as f:
        json.dump(
            dict(metadata, alpaca_snapshot=provenance),
            f,
            indent=2,
            allow_nan=False
        )
    return comparison


def main():
    settings = load_settings()['ibkr']
    parser = argparse.ArgumentParser(
        description='Read-only IBKR diagnostic; orders disabled'
    )
    parser.add_argument('--port', type=int, default=settings['port'])
    parser.add_argument('--client-id', type=int, default=settings['client_id'])
    parser.add_argument(
        '--contract-timeout',
        type=float,
        default=settings['contract_timeout_seconds']
    )
    parser.add_argument(
        '--observe-seconds',
        type=float,
        default=settings['observation_seconds']
    )
    parser.add_argument('--alpaca-snapshot', type=Path)
    parser.add_argument(
        '--output',
        type=Path,
        default=ROOT / 'data' / ('ibkr_shortability_' + datetime.now(
            timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.csv')
    )
    args = parser.parse_args()
    if ShortabilityIB is None:
        parser.exit(2, 'Install requirements-ibkr.txt first.\n')
    # Validate saved comparison input before contacting TWS.
    alpaca_snapshot(args.alpaca_snapshot)
    app = ShortabilityIB()
    (
        rows,
        metadata
    ) = scan(
        app,
        args.port,
        args.client_id,
        args.contract_timeout,
        args.observe_seconds
    )
    comparison = write_comparison(
        rows,
        metadata,
        args.output,
        args.alpaca_snapshot
    )
    print('Saved ' + str(args.output))
    for r in comparison:
        print(
            r['symbol'],
            r['contract_status'],
            r['availability_status'],
            'shares=',
            r['shortable_shares']
        )
    print(json.dumps(metadata, indent=2))
    print(
        (
            'Borrow fee: unsupported TWS field; verify in SLB tool. '
            'NO ORDERS SUBMITTED.'
        )
    )
    return (
        0 if metadata['api_handshake_succeeded']
        and metadata['disconnected']
        and metadata['worker_stopped']
        and (not metadata['worker_error'])
        and (not app.state.fatal.is_set()) else 1
    )
if __name__ == '__main__':
    raise SystemExit(main())
