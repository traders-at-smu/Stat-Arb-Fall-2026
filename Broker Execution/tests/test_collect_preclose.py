# Synthetic provider payloads only. No keys, environment reads or network
# calls.
import base64
from copy import deepcopy
import json
from types import SimpleNamespace
import pandas as pd
import pytest
from strategy import collect_preclose as c


def receipt(
    url,
    params,
    payload,
    when='2026-10-19T09:50:00-04:00',
    status=200
):
    body = json.dumps(payload).encode()
    return (
        dict(method='GET', url=url, params=params, request_started_at=when,
            retrieved_at=when, status=status, body_base64=base64.b64encode(
                body).decode(), response_sha256=c.digest(body))
    )


@pytest.fixture(scope='module')
def stages():
    days = pd.bdate_range(end='2026-10-19', periods=61)
    start = str(days[0].date())
    day = '2026-10-19'
    morning = '2026-10-19T10:00:00-04:00'
    prices_asof = '2026-10-19T15:30:00-04:00'
    calendar = [
        dict(date=str(d.date()), open='09:30', close='16:00') for d in days
    ]
    auctions = {s: [] for s in c.SYMBOLS}
    bars = {s: [] for s in c.SYMBOLS}
    for s in c.SYMBOLS:
        for (i, d) in enumerate(days):
            iso = str(d.date())
            rec = dict(d=iso, o=[], c=[])
            if i > 0:
                rec[
                    'o'
                ] = [
                    dict(t=iso + 'T09:30:00-04:00', x='P', p=100 + i, c='Q',
                        s=100)
                ]
            if i < len(days) - 1:
                rec[
                    'c'
                ] = [
                    dict(t=iso + 'T16:00:00-04:00', x='P', p=101 + i, c='M',
                        s=100)
                ]
            auctions[s].append(rec)
            if i < len(days) - 1:
                bars[
                    s
                ].append(
                    dict(t=iso + 'T00:00:00-04:00', o=100 + i, h=102 + i,
                        l=99 + i, c=101 + i)
                )
    common = dict(symbols=','.join(c.SYMBOLS), feed='sip')
    auctionparams = dict(
        common,
        start=start,
        end=pd.Timestamp(morning).isoformat(),
        limit=10000,
        sort='asc'
    )
    records = [
        receipt(c.PAPER + '/v2/calendar', dict(start=start, end=day),
            calendar),
        receipt(c.DATA + '/v2/stocks/auctions', auctionparams, dict(
            auctions=auctions, next_page_token=None))
    ]
    end = (
        pd.Timestamp(day, tz='America/New_York') - pd.Timedelta(nanoseconds=1)
    ).isoformat()
    for adj in ['raw', 'all']:
        records.append(
            receipt(c.DATA + '/v2/stocks/bars', dict(common, start=start,
                end=end, limit=10000, sort='asc', timeframe='1Day',
                    adjustment=adj), dict(bars=bars, next_page_token=None))
        )
    trades = {
        s: dict(t='2026-10-19T15:29:40-04:00', p=200, x='P', c=[
            '@']) for s in c.SYMBOLS
    }
    price_record = receipt(
        c.DATA + '/v2/stocks/trades/latest',
        common,
        dict(trades=trades),
        '2026-10-19T15:29:50-04:00'
    )
    m = c.collect(
        c.ReplayGET(records),
        'morning',
        day,
        start,
        morning,
        '2026-10-16'
    )
    p = c.collect(
        c.ReplayGET([price_record]),
        'prices',
        day,
        start,
        prices_asof
    )
    policy = dict(
        source='synthetic verified-policy fixture',
        available_at=morning,
        moc_deadline=day + 'T15:50:00-04:00',
        listing_exchanges={s: dict(exchange='P', valid_from=start,
            valid_through=day, available_at=morning,
                source='synthetic listing fixture') for s in c.SYMBOLS}
    )
    return (m, p, policy, prices_asof)


def edit_payload(stage, index, change):
    page = stage['pages'][index]
    payload = deepcopy(page['payload'])
    change(payload)
    r = page['receipt']
    body = json.dumps(payload).encode()
    r.update(
        body_base64=base64.b64encode(body).decode(),
        response_sha256=c.digest(body)
    )
    page['payload'] = payload


def test_complete_market_collection_still_cannot_export(stages, tmp_path):
    report = c.export_snapshot(*stages, tmp_path)
    assert report['market_data_checks_passed']
    assert report['collection_summary']['price_count'] == 32
    assert report['blockers'] == c.UNRESOLVED
    assert not report[
        'snapshot_exported'
    ] and (
        not report['execution_enabled']
    )
    assert [
        p.name for p in tmp_path.iterdir()
    ][
        0
    ].startswith(
        'collection_failure_'
    )
    assert len(list(tmp_path.iterdir())) == 1
    assert 'factor' not in report['collection_summary']


def test_replay_keeps_original_receipts_and_rejects_hash_tampering(stages):
    r = deepcopy(stages[0]['pages'][0]['receipt'])
    page = c.ReplayGET([r]).get(r['url'], r['params'])
    assert page['receipt']['retrieved_at'] == r['retrieved_at']
    assert page[
        'payload'
    ][
        0
    ][
        'date'
    ] != pd.Timestamp(
        r['retrieved_at']
    ).date().isoformat()
    r['body_base64'] = base64.b64encode(b'[]').decode()
    with pytest.raises(ValueError, match='hash'):
        c.verify_receipt(r)


def test_get_transport_archives_exact_bytes_without_headers(tmp_path):
    calls = []

    class HTTP:

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return (
                SimpleNamespace(content=b'{ "trades": {} }', status_code=200)
            )
    t = c.AlpacaGET(
        HTTP(),
        tmp_path,
        clock=lambda : pd.Timestamp('2026-10-19T15:30:00Z')
    )
    result = t.get(
        c.DATA + '/v2/stocks/trades/latest',
        dict(symbols=','.join(c.SYMBOLS), feed='sip')
    )
    assert result[
        'receipt'
    ][
        'response_sha256'
    ] == c.digest(
        b'{ "trades": {} }'
    )
    assert not calls[0][1]['allow_redirects']
    archive = json.loads(next(tmp_path.iterdir()).read_text())
    assert 'headers' not in archive
    assert base64.b64decode(archive['body_base64']) == b'{ "trades": {} }'


@pytest.mark.parametrize(
    'url,params',
    [(c.PAPER + '/v2/orders', {}), ('https://evil.invalid/v2/stocks/auctions',
        {}), (c.DATA + '/v2/stocks/trades/latest', {'feed': 'iex',
            'symbols': ','.join(c.SYMBOLS)}), (
                c.DATA + '/v2/stocks/trades/latest', {'feed': 'sip',
                    'symbols': 'SPY'}), (c.DATA + '/v2/stocks/trades/latest', {
                        'feed': 'sip', 'secret': 'forbidden'})]
)


def test_only_required_get_routes_are_allowed(url, params, tmp_path):

    class NoNetwork:

        def get(self, *a, **k):
            pytest.fail('Rejected request must not reach transport')
    with pytest.raises(ValueError):
        c.AlpacaGET(NoNetwork(), tmp_path).get(url, params)


def test_pagination_requires_all_pages_and_detects_loops():
    url = c.DATA + '/v2/stocks/auctions'
    params = dict(symbols=','.join(c.SYMBOLS), feed='sip')
    first = receipt(url, params, dict(auctions={}, next_page_token='second'))
    second = receipt(
        url,
        dict(params, page_token='second'),
        dict(auctions={}, next_page_token=None)
    )
    assert len(c.pages(c.ReplayGET([first, second]), url, params)) == 2
    with pytest.raises(ValueError):
        c.pages(c.ReplayGET([first]), url, params)
    second = receipt(
        url,
        dict(params, page_token='second'),
        dict(auctions={}, next_page_token='second')
    )
    with pytest.raises(ValueError, match='pagination'):
        c.pages(c.ReplayGET([first, second]), url, params)


@pytest.mark.parametrize(
    'problem',
    ['missing_auction', 'ambiguous_auction', 'wrong_exchange',
        'wrong_condition', 'missing_price', 'stale_price', 'late_price',
            'current_close', 'daily_missing', 'duplicate_daily', 'holiday',
                'early_close', 'late_calendar', 'listing_missing',
                    'listing_expired', 'late_auction', 'late_morning',
                        'deadline', 'wrong_previous', 'payload_tamper',
                            'missing_page']
)


def test_fail_closed_on_invalid_market_inputs(stages, tmp_path, problem):
    (m, p, policy, now) = deepcopy(stages)
    if problem == 'missing_auction':
        edit_payload(m, 1, lambda x: x['auctions'].pop('SPY'))
    if problem == 'ambiguous_auction':
        edit_payload(
            m,
            1,
            lambda x: x['auctions']['SPY'][-1]['o'].append(x['auctions'][
                'SPY'][-1]['o'][0].copy())
        )
    if problem == 'wrong_exchange':
        edit_payload(
            m,
            1,
            lambda x: x['auctions']['SPY'][-1]['o'][0].update(x='N')
        )
    if problem == 'wrong_condition':
        edit_payload(
            m,
            1,
            lambda x: x['auctions']['SPY'][-1]['o'][0].update(c='O')
        )
    if problem == 'missing_price':
        edit_payload(p, 0, lambda x: x['trades'].pop('SPY'))
    if problem == 'stale_price':
        edit_payload(
            p,
            0,
            lambda x: x['trades']['SPY'].update(t='2026-10-19T15:00:00-04:00')
        )
    if problem == 'late_price':
        p['pages'][0]['receipt']['retrieved_at'] = '2026-10-19T15:51:00-04:00'
    if problem == 'current_close':
        edit_payload(
            m,
            1,
            lambda x: x['auctions']['SPY'][-1].update(c=[dict(
                t='2026-10-19T16:00:00-04:00', p=100, x='P', c='M')])
        )
    if problem == 'daily_missing':
        edit_payload(m, 2, lambda x: x['bars'].pop('SPY'))
    if problem == 'duplicate_daily':
        edit_payload(
            m,
            2,
            lambda x: x['bars']['SPY'].append(x['bars']['SPY'][0].copy())
        )
    if problem == 'holiday':
        edit_payload(m, 0, lambda x: x.pop())
    if problem == 'early_close':
        edit_payload(m, 0, lambda x: x[-1].update(close='13:00'))
    if problem == 'late_calendar':
        m['pages'][0]['receipt']['retrieved_at'] = '2026-10-19T11:00:00-04:00'
    if problem == 'listing_missing':
        policy['listing_exchanges'].pop('SPY')
    if problem == 'listing_expired':
        policy['listing_exchanges']['SPY']['valid_through'] = '2026-10-16'
    if problem == 'late_auction':
        m['pages'][1]['receipt']['retrieved_at'] = '2026-10-19T16:00:00-04:00'
    if problem == 'late_morning':
        m['asof'] = '2026-10-19T12:00:00-04:00'
    if problem == 'deadline':
        now = '2026-10-19T15:40:00-04:00'
    if problem == 'wrong_previous':
        m['previous_session'] = '2026-10-15'
    if problem == 'payload_tamper':
        p['pages'][0]['payload']['trades']['SPY']['p'] = 999
    if problem == 'missing_page':
        edit_payload(m, 1, lambda x: x.update(next_page_token='missing'))
    report = c.export_snapshot(m, p, policy, now, tmp_path)
    assert not report['market_data_checks_passed']
    assert not report['snapshot_exported']
    assert all((b in report['blockers'] for b in c.UNRESOLVED))
    assert any((b.startswith('DATA_VALIDATION:') for b in report['blockers']))


def test_unexpected_http_response_is_archived_but_rejected(tmp_path):

    class HTTP:

        def get(self, *a, **k):
            return SimpleNamespace(content=b'{}', status_code=403)
    with pytest.raises(ValueError, match='403'):
        c.AlpacaGET(
            HTTP(),
            tmp_path
        ).get(
            c.PAPER + '/v2/calendar',
            dict(start='2026-10-01', end='2026-10-19')
        )
    assert len(list(tmp_path.iterdir())) == 1


def test_archives_and_reports_never_overwrite(tmp_path):
    path = tmp_path / 'report.json'
    c.write_new(path, {'original': True})
    with pytest.raises(FileExistsError):
        c.write_new(path, {'original': False})
    assert json.loads(path.read_text()) == {'original': True}


def test_early_close_adjusts_submission_deadline(stages):
    (m, p, policy, _) = deepcopy(stages)
    edit_payload(m, 0, lambda x: x[-1].update(close='13:00'))
    edit_payload(
        p,
        0,
        lambda x: [r.update(t='2026-10-19T12:29:40-04:00') for r in x[
            'trades'].values()]
    )
    p['asof'] = '2026-10-19T12:30:00-04:00'
    p[
        'pages'
    ][
        0
    ][
        'receipt'
    ].update(
        request_started_at='2026-10-19T12:29:50-04:00',
        retrieved_at='2026-10-19T12:29:50-04:00'
    )
    policy['moc_deadline'] = '2026-10-19T12:50:00-04:00'
    summary = c.validate_collection(m, p, policy, p['asof'])
    assert summary['submission_deadline'] == '2026-10-19T12:40:00-04:00'


def test_unknown_receipt_time_is_not_replaced_by_replay_clock(stages):
    record = deepcopy(stages[0]['pages'][0]['receipt'])
    del record['retrieved_at']
    with pytest.raises(KeyError):
        c.verify_receipt(record)


def test_get_time_is_recorded_after_response_read(tmp_path):
    events = []

    class HTTP:

        def get(self, *a, **k):
            events.append('get')

            class Response:
                status_code = 200

                @property
                def content(self):
                    events.append('body')
                    return b'[]'
            return Response()

    def clock():
        events.append('clock')
        return pd.Timestamp('2026-10-19T09:00:00Z')
    c.AlpacaGET(
        HTTP(),
        tmp_path,
        clock
    ).get(
        c.PAPER + '/v2/calendar',
        dict(start='2026-10-01', end='2026-10-19')
    )
    assert events == ['clock', 'get', 'body', 'clock']


def test_unauthenticated_request_failures_do_not_expose_session_details(
    tmp_path,
    capsys
):

    class HTTP:

        def get(self, *a, **k):
            raise RuntimeError('sensitive authentication details')
    with pytest.raises(ValueError, match='no response') as exc:
        c.AlpacaGET(
            HTTP(),
            tmp_path
        ).get(
            c.PAPER + '/v2/calendar',
            dict(start='2026-10-01', end='2026-10-19')
        )
    assert 'sensitive' not in str(exc.value)
    assert capsys.readouterr().out == ''


def test_collection_cli_writes_structured_failure_without_snapshot(
    tmp_path,
    monkeypatch
):
    cfg = tmp_path / 'config.json'
    output = tmp_path / 'morning.json'
    cfg.write_text(
        json.dumps(dict(session='2026-10-19', history_start='2016-01-04',
            asof='2026-10-19T10:00:00-04:00', previous_session='2026-10-16'))
    )
    archive = tmp_path / 'empty'
    archive.mkdir()
    monkeypatch.setattr(
        'sys.argv',
        ['collector', 'morning', '--config', str(cfg), '--archive', str(
            archive), '--output', str(output)]
    )
    assert c.main() == 2
    assert not output.exists()
    failure = json.loads(
        next(tmp_path.glob('morning.json.failure.*.json')).read_text()
    )
    assert not failure['snapshot_exported']
    assert all((b in failure['blockers'] for b in c.UNRESOLVED))


def test_full_universe_matches_ian_without_importing_or_editing_research():
    import ast
    from pathlib import Path
    from strategy.config import research_root
    path = research_root() / 'fetch_bars.py'
    if not path.is_file():
        pytest.skip(
            'Private original research intentionally excluded from Git'
        )
    tree = ast.parse(path.read_text())
    tiers = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            if name in ['TIER0', 'TIER1', 'TIER2']:
                tiers[name] = ast.literal_eval(node.value)
    assert list(c.SYMBOLS) == tiers['TIER0'] + tiers['TIER1'] + tiers['TIER2']
    assert len(set(c.SYMBOLS)) == 32
