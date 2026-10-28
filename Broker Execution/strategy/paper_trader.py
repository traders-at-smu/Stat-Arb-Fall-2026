# Supervised paper execution preparation and read-only reconciliation.
#
# No submission path is enabled. Explicit --report selects preclose JSON;
# historical
# CSVs are never consumed. A small account-bound JSON ledger survives restarts.
# Commands: preview-entry, prepare-entry, reconcile, preview-exit,
# prepare-exit.
# The legacy submit commands fail immediately, before reading credentials.
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo
import pandas as pd
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    GetCalendarRequest,
)
ROOT = Path(__file__).resolve().parent.parent
if not __package__:
    sys.path.insert(0, str(ROOT))
from strategy.preview import validate_orders, preflight
from strategy.preclose import timestamp, session_date
from strategy.collect_preclose import SYMBOLS
ET = ZoneInfo('America/New_York')
ENTRY_EXECUTION_LOCK = True
TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}


def broker():
    load_dotenv(ROOT / '.env')
    key = os.getenv('ALPACA_API_KEY')
    secret = os.getenv('ALPACA_SECRET_KEY') or os.getenv('ALPACA_API_SECRET')
    if not key or not secret:
        raise RuntimeError('Missing paper API credentials')
    return TradingClient(key, secret, paper=True)


def value(v):
    return str(getattr(v, 'value', v))


def whole(v):
    q = Decimal(str(v))
    if not q.is_finite() or q < 0 or q != q.to_integral_value():
        raise ValueError('Invalid whole-share quantity')
    return int(q)


def pending_orders(client):
    return (
        client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN))
    )


def load_entry_report(path, now):
    if Path(path).suffix.lower() != '.json':
        raise ValueError(
            'Explicit current-session preclose JSON report required'
        )
    report = json.loads(Path(path).read_text())
    if (
        report.get('mode') != 'PRECLOSE_DRY_RUN'
        or session_date(report['session']).date() != timestamp(now).date()
    ):
        raise ValueError('Historical or wrong-session entry report')
    return report


def check_preflight(client, report, now, limits):
    # Refresh broker state; no cached eligibility Boolean can approve an entry.
    assets = {
        s: client.get_asset(s) for s in {r['symbol'] for r in report['orders']}
    }
    return (
        preflight(report, client.get_account(), assets,
            client.get_all_positions(), pending_orders(client), now, limits,
                SYMBOLS)
    )


def client_order_id(account_id, session, phase, symbol):
    identity = '|'.join(
        ['smu-etf', str(account_id), str(session_date(session).date()), phase,
            symbol]
    )
    return 'smu-' + hashlib.sha256(identity.encode()).hexdigest()[:32]


@contextmanager
def ledger_transaction(path, account_id):
    # Serialize changes and replace atomically; refuse corrupt/foreign ledgers.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(
            path.read_text()
        ) if path.exists() else dict(
            version=1,
            account_id=str(account_id),
            orders={}
        )
        if (
            data.get('version') != 1
            or data.get('account_id') != str(account_id)
            or (not isinstance(data.get('orders'), dict))
        ):
            raise ValueError('Corrupt or foreign-account ledger')
        for (cid, r) in data['orders'].items():
            if (
                cid != r['client_order_id']
                or r['phase'] not in ['entry', 'exit']
                or cid != client_order_id(account_id, r['session'], r['phase'],
                    r['symbol'])
            ):
                raise ValueError('Corrupt ledger identity')
            if (
                r['side'] not in ['BUY', 'SELL']
                or whole(r['requested_qty']) <= 0
                or whole(r['filled_qty']) > whole(r['requested_qty'])
            ):
                raise ValueError('Corrupt ledger quantities')
            origin = session_date(r.get('source_session', r['session']))
            if (
                r['phase'] == 'entry'
                and origin != session_date(r['session'])
                or (r['phase'] == 'exit' and origin >= session_date(r[
                    'session']))
            ):
                raise ValueError('Corrupt ledger position origin')
        yield data
        encoded = json.dumps(data, indent=2, allow_nan=False)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                dir=path.parent,
                delete=False,
                prefix=path.name + '.'
            ) as f:
                temp = f.name
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            temp = None
        finally:
            if temp and Path(temp).exists():
                Path(temp).unlink()


def reserve_orders(
    path,
    account_id,
    session,
    phase,
    orders,
    source_session=None
):
    # Persist intent, not a submission. Any duplicate requires manual review.
    if phase not in ['entry', 'exit']:
        raise ValueError('Invalid ledger phase')
    validate_orders(orders)
    if (
        phase == 'exit'
        and (source_session is None or session_date(
            source_session) >= session_date(session))
    ):
        raise ValueError('Exit requires preceding source session')
    ids = [
        client_order_id(account_id, session, phase,
            r.symbol) for r in orders.itertuples()
    ]
    with ledger_transaction(path, account_id) as ledger:
        if any((cid in ledger['orders'] for cid in ids)):
            raise RuntimeError(
                (
                    'Duplicate preparation/submission; reconcile and '
                    'review, never auto-retry'
                )
            )
        for (cid, r) in zip(ids, orders.itertuples()):
            ledger[
                'orders'
            ][
                cid
            ] = dict(
                client_order_id=cid,
                broker_order_id=None,
                session=str(session_date(session).date()),
                phase=phase,
                symbol=r.symbol,
                side=r.side,
                requested_qty=whole(r.quantity),
                status='prepared',
                filled_qty=0,
                filled_avg_price=None,
                submission_state='prepared',
                source_session=str(session_date(source_session).date(
                    )) if phase == 'exit' else str(session_date(session).date(
                        ))
            )
    return ids


def mark_submission_uncertain(path, account_id, cid):
    # Persist uncertainty BEFORE any separately authorized future submission.
    #
    # This transition sends nothing. Reconciliation is the only safe next
    # action;
    #     a missing broker order must never trigger an automatic retry.
    with ledger_transaction(path, account_id) as ledger:
        row = ledger['orders'][cid]
        if row['submission_state'] != 'prepared':
            raise RuntimeError(
                (
                    'Submission already attempted/observed; reconcile, '
                    'never auto-retry'
                )
            )
        row['submission_state'] = 'uncertain'
        row['attempt_marked_at'] = datetime.now(ET).isoformat()


def entry_requests(df, account_id=None, session=None):
    validate_orders(df)
    return (
        [MarketOrderRequest(symbol=r.symbol, qty=whole(r.quantity),
            side=OrderSide.BUY if r.side == 'BUY' else OrderSide.SELL,
                time_in_force=TimeInForce.CLS, client_order_id=client_order_id(
                    account_id, session, 'entry',
                        r.symbol) if account_id is not None else None) for \
                        r in df.itertuples()]
    )


def reconcile(client, path):
    # Query by deterministic ID, validate cumulative fills, then compare
    # positions.
    #
    # Missing order after an uncertain attempt is ambiguous, never
    # retryable.
    # Rejected orders are recorded as events, not silently resubmitted.
    # Terminal
    # partial fills can be closed once their positions match and no
    # orders remain.
    account_id = str(client.get_account().id)
    blockers = []
    events = []
    expected = {}
    with ledger_transaction(path, account_id) as ledger:
        for (cid, r) in ledger['orders'].items():
            try:
                order = client.get_order_by_client_id(cid)
            except Exception as exc:
                if (
                    getattr(exc, 'status_code', None) == 404
                    and r['submission_state'] == 'prepared'
                ):
                    continue
                blockers.append('Uncertain order lookup: ' + cid)
                continue
            try:
                filled = whole(order.filled_qty)
                requested = whole(order.qty)
                status = value(order.status)
                if (
                    order.symbol != r['symbol']
                    or value(order.side).upper() != r['side']
                    or requested != r['requested_qty']
                    or (str(order.client_order_id) != cid)
                ):
                    raise ValueError('Broker/ledger order identity mismatch')
                if (
                    r['broker_order_id']
                    and str(order.id) != r['broker_order_id']
                ):
                    raise ValueError('Broker order ID changed')
                if (
                    filled < r['filled_qty']
                    or filled > requested
                    or (status == 'filled' and filled != requested)
                    or (status == 'rejected' and filled)
                ):
                    raise ValueError('Inconsistent cumulative fills')
                avg = None if not filled else float(order.filled_avg_price)
                if (
                    filled
                    and (not pd.notna(avg) or not 0 < avg < float('inf'))
                ):
                    raise ValueError('Invalid average fill price')
                r.update(
                    broker_order_id=str(order.id),
                    status=status,
                    filled_qty=filled,
                    filled_avg_price=avg,
                    submission_state='broker_observed',
                    last_reconciled_at=datetime.now(ET).isoformat()
                )
                if status not in TERMINAL:
                    blockers.append(
                        'Nonterminal order requires review: ' + cid
                    )
                if filled < requested and filled:
                    events.append('Partial fill: ' + cid)
                if status == 'rejected':
                    events.append('Rejected order: ' + cid)
            except (ValueError, TypeError, AttributeError, ArithmeticError):
                blockers.append('Invalid broker order state: ' + cid)
        for r in ledger['orders'].values():
            expected[
                r['symbol']
            ] = expected.get(
                r['symbol'],
                0
            ) + (
                1 if r['side'] == 'BUY' else -1
            ) * whole(
                r['filled_qty']
            )
        try:
            actual = {}
            for p in client.get_all_positions():
                q = Decimal(str(p.qty))
                if (
                    not q.is_finite()
                    or q != q.to_integral_value()
                    or p.symbol in actual
                ):
                    raise ValueError('Invalid account position')
                actual[p.symbol] = int(q)
            for symbol in set(expected) | set(actual):
                if expected.get(symbol, 0) != actual.get(symbol, 0):
                    blockers.append(
                        'Strategy/account position discrepancy: ' + symbol
                    )
            if pending_orders(client):
                blockers.append('Open account orders remain')
        except Exception:
            blockers.append('Account positions/open orders unavailable')
            actual = {}
        ledger[
            'reconciliation'
        ] = dict(
            blockers=blockers,
            events=events,
            expected_positions=expected,
            account_positions=actual,
            checked_at=datetime.now(ET).isoformat(),
            automatic_retry_allowed=False
        )
        result = dict(ledger['reconciliation'])
    return result


def exit_requests(client, path, exit_session, previous_session):
    # Caller supplies calendar-confirmed sessions; exits only reconciled
    # strategy fills.
    (
        exit_day,
        previous
    ) = (
        session_date(exit_session),
        session_date(previous_session)
    )
    if previous >= exit_day:
        raise ValueError('Invalid exit sessions')
    # Confirm the immediately preceding market session, including holidays.
    calendar = client.get_calendar(
        filters=GetCalendarRequest(start=previous.date(), end=exit_day.date())
    )
    dates = [session_date(str(r.date)) for r in calendar]
    if dates != [previous, exit_day]:
        raise ValueError('Exit is not next calendar trading session')
    state = reconcile(client, path)
    if state['blockers']:
        raise RuntimeError('; '.join(state['blockers']))
    account_id = str(client.get_account().id)
    with ledger_transaction(path, account_id) as ledger:
        ownership = {}
        for r in ledger['orders'].values():
            origin = r.get('source_session', r['session'])
            key = (origin, r['symbol'])
            ownership[
                key
            ] = ownership.get(
                key,
                0
            ) + (
                1 if r['side'] == 'BUY' else -1
            ) * r[
                'filled_qty'
            ]
        if (
            any((q and session_date(origin) != previous for ((origin, symbol),
                q) in ownership.items()))
        ):
            raise RuntimeError(
                'Position origin is not the preceding entry session'
            )
        orders = []
        for (symbol, q) in state['expected_positions'].items():
            if not q:
                continue
            cid = client_order_id(account_id, exit_day, 'exit', symbol)
            if cid in ledger['orders']:
                raise RuntimeError(
                    'Exit already prepared; reconcile, never auto-retry'
                )
            orders.append(
                MarketOrderRequest(symbol=symbol, qty=abs(q),
                    side=OrderSide.SELL if q > 0 else OrderSide.BUY,
                        time_in_force=TimeInForce.OPG, client_order_id=cid)
            )
    return orders


def show(requests):
    for r in requests:
        print(
            f'{value(r.side).upper():4}' \
                f' ' \
                f'{int(r.qty):6}' \
                f' ' \
                f'{r.symbol:6}' \
                f' ' \
                f'{value(r.time_in_force)}'
        )
    print(f'Prepared {len(requests)} requests; NO ORDERS SUBMITTED')


def main():
    parser = argparse.ArgumentParser(
        description=(
            'Supervised paper preparation and read-only '
            'reconciliation; submission disabled'
        )
    )
    parser.add_argument(
        'command',
        choices=['preview-entry', 'prepare-entry', 'reconcile', 'preview-exit',
            'prepare-exit', 'submit-entry', 'submit-exit']
    )
    parser.add_argument('--report', type=Path)
    parser.add_argument(
        '--ledger',
        type=Path,
        default=ROOT / 'data' / 'paper_order_ledger.json'
    )
    parser.add_argument(
        '--limits',
        type=Path,
        help=(
            'Explicit '
            'account_id/max_gross/max_symbol/buying_power_buffer '
            'JSON'
        )
    )
    parser.add_argument('--session')
    parser.add_argument('--previous-session')
    args = parser.parse_args()
    if args.command.startswith('submit'):
        raise RuntimeError(
            (
                'PAPER EXECUTION LOCKED: submission is disabled for '
                'both entry and exit'
            )
        )
    now = datetime.now(ET)
    report = None
    if args.command.endswith('entry'):
        if not args.report or not args.limits:
            parser.error('Entry requires explicit --report and --limits')
        report = load_entry_report(args.report, now)
    if (
        args.command.endswith('exit')
        and (not args.session or not args.previous_session)
    ):
        parser.error('Exit requires calendar session and previous-session')
    client = broker()
    if report is not None:
        failures = check_preflight(
            client,
            report,
            now,
            json.loads(args.limits.read_text())
        )
        if failures:
            raise RuntimeError('ENTRY BLOCKED: ' + '; '.join(failures))
        # Even after future data clearance, reconcile any existing
        # ledger first.
        state = reconcile(client, args.ledger)
        if state['blockers']:
            raise RuntimeError('Ledger reconciliation blocked')
        requests = entry_requests(
            pd.DataFrame(report['orders']),
            str(client.get_account().id),
            report['session']
        )
        if args.command == 'prepare-entry':
            reserve_orders(
                args.ledger,
                str(client.get_account().id),
                report['session'],
                'entry',
                pd.DataFrame(report['orders'])
            )
        show(requests)
    elif args.command == 'reconcile':
        print(json.dumps(reconcile(client, args.ledger), indent=2))
    else:
        requests = exit_requests(
            client,
            args.ledger,
            args.session,
            args.previous_session
        )
        if args.command == 'prepare-exit' and requests:
            orders = pd.DataFrame(
                [dict(symbol=r.symbol, side=value(r.side).upper(),
                    quantity=r.qty) for r in requests]
            )
            reserve_orders(
                args.ledger,
                str(client.get_account().id),
                args.session,
                'exit',
                orders,
                args.previous_session
            )
        show(requests)
if __name__ == '__main__':
    main()
