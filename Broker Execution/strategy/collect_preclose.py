# Read-only pre-close collection; no environment, dotenv, auth, or order
# access.
#
# Use via `python -m strategy.collect_preclose --help`. Replay archives
# preserve the
# ORIGINAL receipt times. AlpacaGET accepts an externally configured HTTP
# session;
# this module never discovers credentials. It permits only four exact GET
# routes,
# disables redirects, and never archives headers. No authenticated calls are
# made
# by tests. A hash detects alteration, not a forged receipt or provider
# provenance.
#
# Morning collection: calendar, SIP auctions, raw/all daily bars through
# previous
# session. Price collection: separate latest SIP trades. Request receipt time
# is
# recorded AFTER the full body is received. Provider `t` remains source/SIP
# time,
# not a claim about exchange publication or when the provider first knew a
# value.
#
# No verified factor source or approved inception-aware initialization policy
# exists
# in this project. Those blockers are unconditional: collection can succeed,
# but
# snapshot export cannot. No flags can bypass them. Daily-bar ratios are
# diagnostic
# only; they never supply factor/factor_available_at or replace auction prices.
import argparse
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import numpy as np
import pandas as pd
from .preclose import session_date, timestamp
SYMBOLS = tuple(
    (
        'SPY XLK XLC XLY XLP XLE XLF XLV XLI XLB XLRE XLU SMH '
        'IGV HACK XRT XHB ITB XOP OIH KRE KBE KIE IAI XBI IHI '
        'XPH ITA JETS IYT XME GDX'
    ).split()
)
DATA = 'https://data.alpaca.markets'
PAPER = 'https://paper-api.alpaca.markets'
ROUTES = {
    DATA + '/v2/stocks/auctions': {'symbols', 'start', 'end', 'feed', 'limit',
        'page_token', 'sort'},
    DATA + '/v2/stocks/bars': {'symbols', 'start', 'end', 'feed', 'limit',
        'page_token', 'sort', 'timeframe', 'adjustment'},
    DATA + '/v2/stocks/trades/latest': {'symbols', 'feed'},
    PAPER + '/v2/calendar': {'start', 'end'}
}
UNRESOLVED = [
    'ADJUSTMENT_FACTOR_PROVENANCE_UNVERIFIED',
    'HISTORICAL_INITIALIZATION_UNVERIFIED'
]


def utc_now():
    return pd.Timestamp.now(tz='UTC')


def check_request(url, params):
    if url not in ROUTES or set(params) - ROUTES[url] or urlsplit(url).query:
        raise ValueError('GET endpoint or request parameters not allowlisted')
    if (
        'symbols' in params
        and set(params['symbols'].split(',')) != set(SYMBOLS)
    ):
        raise ValueError('Request must contain the full 32-ticker universe')
    if url.startswith(DATA) and params.get('feed') != 'sip':
        raise ValueError('Explicit SIP required; no feed substitution')


def digest(body):
    return hashlib.sha256(body).hexdigest()


def write_new(path, obj):
    # Never overwrite a previous receipt, report, or snapshot.
    encoded = json.dumps(obj, indent=2, allow_nan=False)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('x') as f:
        f.write(encoded)


class AlpacaGET:
    # Inject a requests-compatible session; caller owns authentication
    # externally.

    def __init__(self, session, archive_dir, clock=utc_now):
        (
            self.session,
            self.root,
            self.clock
        ) = (
            session,
            Path(archive_dir),
            clock
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, url, params):
        check_request(url, params)
        started = timestamp(self.clock()).isoformat()
        try:
            response = self.session.get(
                url,
                params=dict(params),
                timeout=30,
                allow_redirects=False
            )
            body = bytes(response.content)
            received = timestamp(self.clock()).isoformat()
        except Exception:
            # Never include exception text that might contain auth or headers.
            raise ValueError(
                'Read-only data request failed; no response received'
            ) from None
        record = dict(
            method='GET',
            url=url,
            params=dict(params),
            request_started_at=started,
            retrieved_at=received,
            status=int(response.status_code),
            response_sha256=digest(body),
            body_base64=base64.b64encode(body).decode(),
            source_timestamp_semantics=(
                'Provider t is source/SIP time; retrieved_at is local '
                'full-response receipt'
            )
        )
        write_new(self.root / (uuid4().hex + '.json'), record)
        return verify_receipt(record)


def verify_receipt(record):
    check_request(record['url'], record['params'])
    if record['method'] != 'GET':
        raise ValueError('Non-GET archive rejected')
    body = base64.b64decode(record['body_base64'], validate=True)
    if digest(body) != record['response_sha256']:
        raise ValueError('Archived response hash mismatch')
    if (
        timestamp(record['request_started_at']) > timestamp(record[
            'retrieved_at'])
    ):
        raise ValueError('Invalid retrieval chronology')
    if record['status'] != 200:
        raise ValueError('Provider returned HTTP ' + str(record['status']))
    payload = json.loads(body)
    return dict(receipt=deepcopy(record), payload=payload)


class ReplayGET:
    # Exact request matching, including pagination; receipt times never
    # rewritten.

    def __init__(self, records):
        self.records = deepcopy(records)
        self.used = set()

    @classmethod
    def from_directory(cls, directory):
        return (
            cls([json.loads(p.read_text()) for p in sorted(Path(
                directory).glob('*.json'))])
        )

    def get(self, url, params):
        check_request(url, params)
        matches = [
            (i, r) for (i, r) in enumerate(self.records) if i not in self.used
            and r['url'] == url
            and (r['params'] == params)
        ]
        if len(matches) != 1:
            raise ValueError(
                'Missing or ambiguous archived request (including page token)'
            )
        (i, record) = matches[0]
        self.used.add(i)
        return verify_receipt(record)


def pages(transport, url, params):
    (result, seen, token) = ([], set(), None)
    for _ in range(10000):
        query = dict(params)
        if token is not None:
            query['page_token'] = token
        page = transport.get(url, query)
        result.append(page)
        token = page[
            'payload'
        ].get(
            'next_page_token'
        ) if isinstance(
            page['payload'],
            dict
        ) else None
        if not token:
            return result
        if not isinstance(token, str) or token in seen:
            raise ValueError('Repeated or invalid pagination token')
        seen.add(token)
    raise ValueError('Pagination limit exceeded')


def collect(
    transport,
    phase,
    session,
    history_start,
    asof,
    previous_session=None
):
    # Collect receipts only; validation/export is a separate fail-closed step.
    cutoff = timestamp(asof)
    (day, start) = (session_date(session), session_date(history_start))
    if cutoff.date() != day.date() or start >= day:
        raise ValueError('Invalid collection session or history start')
    common = dict(symbols=','.join(SYMBOLS), feed='sip')
    if phase == 'morning':
        # Previous session must subsequently match the fetched
        # calendar exactly.
        previous = session_date(previous_session)
        if previous >= day:
            raise ValueError('Invalid previous session')
        queries = [
            (PAPER + '/v2/calendar', dict(start=str(start.date()), end=str(
                day.date()))),
            (DATA + '/v2/stocks/auctions', dict(common, start=str(start.date(
                )), end=cutoff.isoformat(), limit=10000, sort='asc'))
        ]
        for adjustment in ['raw', 'all']:
            # Bound at next midnight: no partial current-session daily bar.
            end = pd.Timestamp(
                str(day.date()),
                tz='America/New_York'
            ) - pd.Timedelta(
                nanoseconds=1
            )
            queries.append(
                (DATA + '/v2/stocks/bars', dict(common, start=str(start.date(
                    )), end=end.isoformat(), limit=10000, sort='asc',
                        timeframe='1Day', adjustment=adjustment))
            )
    elif phase == 'prices':
        queries = [(DATA + '/v2/stocks/trades/latest', common)]
    else:
        raise ValueError('Phase must be morning or prices')
    collected = []
    for (url, params) in queries:
        collected.extend(pages(transport, url, params))
    if (
        any((timestamp(p['receipt'][
            'retrieved_at']) > cutoff for p in collected))
    ):
        raise ValueError('Collection completed after supplied as-of cutoff')
    return (
        dict(phase=phase, session=str(day.date()), history_start=str(
            start.date()), asof=cutoff.isoformat(), previous_session=str(
                previous.date()) if phase == 'morning' else None,
                    pages=collected)
    )


def finite_price(value):
    if (
        isinstance(value, bool)
        or not np.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError('Missing/nonpositive/nonfinite price')
    return float(value)


def validate_collection(morning, prices, policy, now):
    # Market-data diagnostics only. Returned data deliberately has NO factors.
    now = timestamp(now)
    day = session_date(morning['session'])
    start = session_date(morning['history_start'])
    cutoff = timestamp(morning['asof'])
    if (
        prices['phase'] != 'prices'
        or morning['phase'] != 'morning'
        or prices['session'] != morning['session']
    ):
        raise ValueError('Morning and price stages do not match')
    if (
        prices['history_start'] != morning['history_start']
        or timestamp(prices['asof']) > now
    ):
        raise ValueError('Price collection history/as-of mismatch')
    checked = []
    for stage in [morning, prices]:
        # Re-run exact request/pagination planning against the
        # supplied receipts.
        # A hand-edited stage cannot hide a missing final page or
        # swap endpoints.
        replay = ReplayGET([p['receipt'] for p in stage['pages']])
        planned = collect(
            replay,
            stage['phase'],
            stage['session'],
            stage['history_start'],
            stage['asof'],
            stage.get('previous_session')
        )
        if len(planned['pages']) != len(stage['pages']):
            raise ValueError('Unexpected or incomplete stage receipt chain')
        for page in stage['pages']:
            verified = verify_receipt(page['receipt'])
            if verified['payload'] != page['payload']:
                raise ValueError(
                    'Normalized payload differs from archived response'
                )
            if (
                timestamp(page['receipt']['retrieved_at']) > timestamp(stage[
                    'asof'])
            ):
                raise ValueError('Receipt unavailable at stage cutoff')
            checked.append(page)
    calpages = [
        p for p in morning['pages'] if p['receipt'][
            'url'] == PAPER + '/v2/calendar'
    ]
    if len(calpages) != 1:
        raise ValueError('Missing or ambiguous calendar')
    rows = calpages[0]['payload']
    schedule = {}
    for row in rows:
        date = session_date(row['date'])
        iso = str(date.date())
        op = pd.Timestamp(iso + ' ' + row['open'], tz='America/New_York')
        cl = pd.Timestamp(iso + ' ' + row['close'], tz='America/New_York')
        if date in schedule or date.dayofweek >= 5 or (not op < cl):
            raise ValueError('Invalid/duplicate calendar session')
        schedule[date] = (op, cl)
    dates = sorted(schedule)
    if (
        day not in schedule
        or start not in schedule
        or len(dates) < 61
        or (dates[-1] != day)
        or (dates[0] != start)
    ):
        raise ValueError(
            'Holiday, missing calendar coverage, or insufficient history'
        )
    if session_date(morning['previous_session']) != dates[-2]:
        raise ValueError('Previous session differs from calendar')
    (op, cl) = schedule[day]
    # Policy supplied separately; no guessed per-venue deadline or listing map.
    if not policy['source'] or timestamp(policy['available_at']) > cutoff:
        raise ValueError('Unverifiable deadline/listing policy')
    deadline = min(
        timestamp(policy['moc_deadline']),
        cl - pd.Timedelta(minutes=20)
    )
    if (
        deadline.date() != day.date()
        or not op <= cutoff <= op + pd.Timedelta(hours=2)
        or (not cutoff <= now < deadline)
        or (now.date() != day.date())
    ):
        raise ValueError(
            'Wrong session, late morning, or MOC deadline reached'
        )
    mappings = policy['listing_exchanges']
    if set(mappings) != set(SYMBOLS):
        raise ValueError('Missing 32-ticker listing-exchange mapping')
    auctions = {}
    bars = {'raw': {}, 'all': {}}
    latest = {}
    for page in checked:
        (receipt, payload) = (page['receipt'], page['payload'])
        url = receipt['url']
        received = timestamp(receipt['retrieved_at'])
        if url == DATA + '/v2/stocks/auctions':
            if received > cutoff:
                raise ValueError('Late auction retrieval')
            for (symbol, days) in payload['auctions'].items():
                if symbol not in SYMBOLS:
                    raise ValueError('Unexpected auction ticker')
                mapping = mappings[symbol]
                if (
                    not mapping['source']
                    or timestamp(mapping['available_at']) > cutoff
                ):
                    raise ValueError('Unverifiable listing-exchange mapping')
                for record in days:
                    date = session_date(record['d'])
                    if (
                        date not in schedule
                        or not session_date(mapping[
                            'valid_from']) <= date <= session_date(mapping[
                                'valid_through'])
                    ):
                        raise ValueError(
                            'Auction outside calendar/listing-map coverage'
                        )
                    for (
                        kind,
                        key,
                        condition
                    ) in [
                        ('open', 'o', 'Q'),
                        ('close', 'c', 'M')
                    ]:
                        prints = record.get(key, [])
                        if date == day and kind == 'close' and prints:
                            raise ValueError(
                                'Current closing auction cannot be used'
                            )
                        required = kind == 'close' if date == start else \
                        kind == 'open' if date == day else True
                        if not required:
                            continue
                        candidates = [
                            p for p in prints if p['x'] == mapping['exchange']
                            and p['c'] == condition
                        ]
                        if len(candidates) != 1:
                            raise ValueError(
                                'Missing/ambiguous official auction identity'
                            )
                        p = candidates[0]
                        event = timestamp(p['t'])
                        expected = schedule[date][0 if kind == 'open' else 1]
                        if (
                            not expected <= event <= expected + pd.Timedelta(
                                minutes=5)
                            or not event <= received <= cutoff
                        ):
                            raise ValueError(
                                'Invalid auction event/receipt time'
                            )
                        identity = (date, symbol, kind)
                        if identity in auctions:
                            raise ValueError('Duplicate auction observation')
                        auctions[
                            identity
                        ] = dict(
                            session=str(date.date()),
                            symbol=symbol,
                            kind=kind,
                            price=finite_price(p['p']),
                            observed_at=p['t'],
                            retrieved_at=receipt['retrieved_at'],
                            available_at=receipt['retrieved_at'],
                            exchange=p['x'],
                            condition=p['c'],
                            response_sha256=receipt['response_sha256']
                        )
        elif url == DATA + '/v2/stocks/bars':
            adjustment = receipt['params']['adjustment']
            for (symbol, records) in payload['bars'].items():
                if symbol not in SYMBOLS:
                    raise ValueError('Unexpected daily ticker')
                for r in records:
                    date = timestamp(r['t']).tz_localize(None).normalize()
                    if date not in schedule or date >= day:
                        raise ValueError(
                            'Daily bar outside completed calendar sessions'
                        )
                    identity = (date, symbol)
                    if identity in bars[adjustment]:
                        raise ValueError('Duplicate daily bar')
                    if timestamp(r['t']) > received or received > cutoff:
                        raise ValueError('Invalid daily receipt time')
                    for field in ['o', 'h', 'l', 'c']:
                        finite_price(r[field])
                    bars[adjustment][identity] = r
        elif url == DATA + '/v2/stocks/trades/latest':
            for (symbol, r) in payload['trades'].items():
                if symbol not in SYMBOLS or symbol in latest:
                    raise ValueError('Unexpected/duplicate price ticker')
                event = timestamp(r['t'])
                if (
                    not op <= event <= received <= now
                    or now - event > pd.Timedelta(seconds=60)
                ):
                    raise ValueError(
                        'Missing/stale/late regular-session sizing price'
                    )
                if set(r.get('c', [])) & {'T', 'U', 'Z', '4', '7', '9'}:
                    raise ValueError('Ineligible sizing trade condition')
                latest[
                    symbol
                ] = dict(
                    symbol=symbol,
                    price=finite_price(r['p']),
                    observed_at=r['t'],
                    retrieved_at=receipt['retrieved_at'],
                    available_at=receipt['retrieved_at']
                )
    expected_auctions = {
        (d, s, k) for d in dates for s in SYMBOLS for k in ([
            'close'] if d == start else ['open'] if d == day else ['open',
                'close'])
    }
    expected_bars = {(d, s) for d in dates[:-1] for s in SYMBOLS}
    if set(auctions) != expected_auctions:
        raise ValueError(
            'Missing auction coverage (including pre-inception history)'
        )
    if any((set(b) != expected_bars for b in bars.values())):
        raise ValueError('Missing raw/adjusted daily coverage')
    if set(latest) != set(SYMBOLS):
        raise ValueError('Missing 32-ticker sizing prices')
    # Numerical diagnostics are not verified as-of adjustment factors.
    for identity in expected_bars:
        ratio = float(
            bars['all'][identity]['c']
        ) / float(
            bars['raw'][identity]['c']
        )
        finite_price(ratio)
    return (
        dict(auction_count=len(auctions), daily_bar_count={k: len(v) for (k,
            v) in bars.items()}, price_count=len(latest),
                submission_deadline=deadline.isoformat())
    )


def export_snapshot(morning, prices, policy, now, output_directory):
    # Only a failure report is currently possible: unresolved blockers
    # cannot be bypassed.
    root = Path(output_directory)
    root.mkdir(parents=True, exist_ok=True)
    report = dict(
        snapshot_exported=False,
        execution_approved=False,
        execution_enabled=False,
        blockers=UNRESOLVED.copy(),
        market_data_checks_passed=False
    )
    try:
        report[
            'collection_summary'
        ] = validate_collection(
            morning,
            prices,
            policy,
            now
        )
        report['market_data_checks_passed'] = True
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        OverflowError
    ) as exc:
        report['blockers'].append('DATA_VALIDATION: ' + str(exc))
    # No factors, initialization bypass, placeholder snapshot, or
    # approval flags.
    report[
        'detail'
    ] = (
        'Snapshot withheld: verified as-of factors and '
        'initialization compatibility are not implemented.'
    )
    write_new(root / ('collection_failure_' + uuid4().hex + '.json'), report)
    return report


def main():
    parser = argparse.ArgumentParser(
        description='Read-only pre-close collection and offline replay'
    )
    parser.add_argument('phase', choices=['morning', 'prices', 'assemble'])
    parser.add_argument(
        '--archive',
        type=Path,
        help='Directory of original response receipts (offline replay)'
    )
    parser.add_argument(
        '--config',
        type=Path,
        required=True,
        help='Non-secret session/history/asof/previous_session/policy JSON'
    )
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--morning', type=Path)
    parser.add_argument('--prices', type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        if args.phase == 'assemble':
            report = export_snapshot(
                json.loads(args.morning.read_text()),
                json.loads(args.prices.read_text()),
                config['policy'],
                config['asof'],
                args.output
            )
            print(json.dumps(report, indent=2))
            return 2
        if not args.archive:
            raise ValueError(
                (
                    'Replay archive required; authenticated GET transport '
                    'is injected via Python API'
                )
            )
        stage = collect(
            ReplayGET.from_directory(args.archive),
            args.phase,
            config['session'],
            config['history_start'],
            config['asof'],
            config.get('previous_session')
        )
        write_new(args.output, stage)
        print(
            'Collected ' + args.phase + ' receipts; snapshot export rem' \
                'ains blocked.'
        )
        return 0
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
        # Provider/auth exception content is never printed by the
        # network adapter.
        report = dict(
            snapshot_exported=False,
            execution_enabled=False,
            blockers=UNRESOLVED + ['COLLECTION_FAILED: ' + str(exc)]
        )
        failure = args.output.with_name(
            args.output.name + '.failure.' + uuid4().hex + '.json'
        )
        write_new(failure, report)
        print('Collection failed; see ' + str(failure))
        return 2
if __name__ == '__main__':
    raise SystemExit(main())
