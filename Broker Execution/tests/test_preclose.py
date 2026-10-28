# Synthetic as-known inputs; calendar provenance itself is not certified here.
from copy import deepcopy
from types import SimpleNamespace
import json
import numpy as np
import pandas as pd
import pytest
from strategy.preclose import build_preclose_preview, run_snapshot


@pytest.fixture
def inputs():
    days = pd.bdate_range(end='2026-10-19', periods=61)
    morning = '2026-10-19T10:00:00-04:00'
    (rows, sessions) = ([], [])
    for (i, d) in enumerate(days):
        iso = str(d.date())
        sessions.append(
            dict(session=iso, open=iso + 'T09:30:00-04:00',
                close=iso + 'T16:00:00-04:00',
                    moc_deadline=iso + 'T15:50:00-04:00')
        )
        for symbol in ['SPY', 'ETF']:
            kinds = [
                'close'
            ] if i == 0 else [
                'open'
            ] if i == 60 else [
                'open',
                'close'
            ]
            for kind in kinds:
                obs = iso + (
                    'T09:30:00-04:00' if kind == 'open' else 'T16:00:00-04:00'
                )
                rows.append(
                    dict(session=iso, symbol=symbol, kind=kind,
                        price=100 + i + (symbol == 'ETF'), factor=1.0,
                            observed_at=obs, available_at=obs,
                                factor_available_at=obs)
                )
    snapshot = dict(
        session='2026-10-19',
        history_start=str(days[0].date()),
        morning_asof=morning,
        calendar=dict(coverage_start=str(days[0].date()),
            coverage_end='2026-10-19', available_at=str(days[0].date(
                )) + 'T08:00:00-04:00', source='synthetic fixture',
                    sessions=sessions),
        auctions=rows,
        prices=[dict(symbol=s, price=100,
            observed_at='2026-10-19T15:29:40-04:00',
                available_at='2026-10-19T15:29:45-04:00') for s in ['SPY',
                    'ETF']]
    )
    calls = []

    def seg(r):
        calls.append(r.copy())
        return (
            (pd.DataFrame(1.0, index=r.index, columns=['ETF']), pd.DataFrame(
                0.0, index=r.index, columns=['ETF']))
        )
    pc = SimpleNamespace(
        SYMS=['SPY', 'ETF'],
        ALL=['ETF'],
        seg_betas=seg,
        spy_panel=lambda r: r[['SPY']].rename(columns={'SPY': 'ETF'}),
        parent_panel=lambda r: pd.DataFrame(0.0, index=r.index, columns=[
            'ETF']),
        resid=lambda r,
        s,
        p,
        b1,
        b2: r - b1 * s - b2 * p
    )

    def weights(e, u):
        assert len(e) == 61 and e.index[-1] == pd.Timestamp('2026-10-19')
        return (
            (pd.DataFrame(1.0, index=e.index, columns=u), pd.Series(True,
                index=e.index))
        )
    model = SimpleNamespace(
        PC=pc,
        weights=weights,
        netted=lambda w,
        b1,
        b2,
        u: pd.Series({'SPY': -1.0, 'ETF': 1.0})
    )
    return (model, snapshot, calls)


def run(inputs, snapshot=None, now='2026-10-19T15:30:00-04:00'):
    (model, original, _) = inputs
    result = build_preclose_preview(model, snapshot or original, now)
    assert result['execution_approved'] is False
    assert result['execution_enabled'] is False
    return result


def test_valid_snapshot_uses_timestamped_prices_and_prefix(inputs):
    result = run(inputs)
    assert result['data_checks_passed']
    assert result['submission_deadline'] == '2026-10-19T15:40:00-04:00'
    assert {o['reference_price'] for o in result['orders']} == {100}
    assert {o['quantity'] for o in result['orders']} == {1000}
    assert all((not o['execution_approved'] for o in result['orders']))
    assert inputs[2][0].index.max() == pd.Timestamp('2026-10-19')
    assert inputs[2][1].iloc[-1].isna().all()
    snapshot = deepcopy(inputs[1])
    snapshot['prices'][0]['price'] = 200
    changed = run(inputs, snapshot)
    assert changed['orders'][0]['quantity'] == 500


@pytest.mark.parametrize(
    'problem',
    ['missing_close', 'missing_open', 'missing_factor', 'late_auction',
        'late_factor', 'late_price', 'stale_price', 'future_price',
            'duplicate', 'future_close', 'missing_calendar', 'holiday',
                'early_close', 'after_deadline', 'missing_timestamp',
                    'late_calendar', 'future_history', 'wrong_date',
                        'missing_price', 'missing_history', 'late_morning']
)


def test_invalid_snapshots_never_generate_approved_portfolio(inputs, problem):
    snap = deepcopy(inputs[1])
    now = '2026-10-19T15:30:00-04:00'
    if problem == 'missing_close':
        snap[
            'auctions'
        ] = [
            r for r in snap['auctions'] if not (r['session'] == '2026-10-16'
                and r['kind'] == 'close')
        ]
    if problem == 'missing_open':
        snap['auctions'].pop()
    if problem == 'missing_factor':
        del snap['auctions'][-1]['factor']
    if problem == 'late_auction':
        snap['auctions'][-1]['available_at'] = '2026-10-19T15:51:00-04:00'
    if problem == 'late_factor':
        snap[
            'auctions'
        ][
            -1
        ][
            'factor_available_at'
        ] = '2026-10-19T16:00:00-04:00'
    if problem == 'late_price':
        snap['prices'][0]['available_at'] = '2026-10-19T15:51:00-04:00'
    if problem == 'stale_price':
        snap['prices'][0]['observed_at'] = '2026-10-19T15:00:00-04:00'
    if problem == 'future_price':
        snap['prices'][0]['observed_at'] = '2026-10-19T16:00:00-04:00'
    if problem == 'duplicate':
        snap['auctions'].append(snap['auctions'][-1])
    if problem == 'future_close':
        row = deepcopy(snap['auctions'][-1])
        row['kind'] = 'close'
        snap['auctions'].append(row)
    if problem in ['holiday', 'missing_calendar']:
        snap['calendar']['sessions'].pop()
    if problem == 'early_close':
        snap['calendar']['sessions'][-1]['close'] = '2026-10-19T13:00:00-04:00'
        snap[
            'calendar'
        ][
            'sessions'
        ][
            -1
        ][
            'moc_deadline'
        ] = '2026-10-19T12:50:00-04:00'
    if problem == 'after_deadline':
        now = '2026-10-19T15:40:00-04:00'
    if problem == 'missing_timestamp':
        del snap['prices'][0]['available_at']
    if problem == 'late_calendar':
        snap['calendar']['available_at'] = '2026-10-19T16:00:00-04:00'
    if problem == 'future_history':
        snap['auctions'][-1]['session'] = '2026-10-20'
    if problem == 'wrong_date':
        now = '2026-10-20T15:30:00-04:00'
    if problem == 'missing_price':
        snap['prices'].pop()
    if problem == 'missing_history':
        snap['auctions'] = snap['auctions'][2:]
    if problem == 'late_morning':
        snap['morning_asof'] = '2026-10-19T15:00:00-04:00'
    result = run(inputs, snap, now)
    assert not result['data_checks_passed']
    assert result['blockers'] and result['orders'] == []
    # fail before calling the model
    assert inputs[2] == []


def test_early_close_uses_relative_deadline(inputs):
    snap = deepcopy(inputs[1])
    snap[
        'calendar'
    ][
        'sessions'
    ][
        -1
    ].update(
        close='2026-10-19T13:00:00-04:00',
        moc_deadline='2026-10-19T12:50:00-04:00'
    )
    for row in snap['prices']:
        row.update(
            observed_at='2026-10-19T12:29:40-04:00',
            available_at='2026-10-19T12:29:45-04:00'
        )
    result = run(inputs, snap, '2026-10-19T12:30:00-04:00')
    assert result['data_checks_passed']
    assert result['submission_deadline'] == '2026-10-19T12:40:00-04:00'


def test_snapshot_writer_does_not_approve(inputs, tmp_path):
    (inp, out) = (tmp_path / 'snapshot.json', tmp_path / 'report.json')
    inp.write_text(json.dumps(inputs[1]))
    run_snapshot(inputs[0], inp, out, '2026-10-19T15:30:00-04:00')
    report = json.loads(out.read_text())
    assert report['data_checks_passed'] and (not report['execution_approved'])


def test_cli_preclose_never_loads_credentials_or_contacts_broker(
    inputs,
    tmp_path,
    monkeypatch
):
    import runpy
    import sys
    from pathlib import Path
    import dotenv
    import alpaca.trading.client
    (inp, out) = (tmp_path / 'snapshot.json', tmp_path / 'report.json')
    inp.write_text(json.dumps(inputs[1]))

    def forbidden(*args, **kwargs):
        pytest.fail(
            'Pre-close workflow must not load credentials or create a broker'
        )
    monkeypatch.setattr(dotenv, 'load_dotenv', forbidden)
    monkeypatch.setattr(alpaca.trading.client, 'TradingClient', forbidden)
    monkeypatch.setitem(sys.modules, 'forward_test', inputs[0])
    monkeypatch.setattr(
        sys,
        'argv',
        ['main.py', '--dry-run', '--preclose-snapshot', str(inp),
            '--as-of-replay', '2026-10-19T15:30:00-04:00', '--preclose-output',
                str(out)]
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            str(Path(__file__).resolve().parents[1] / 'main.py'),
            run_name='__main__'
        )
    assert exc.value.code == 0
    assert not json.loads(out.read_text())['execution_approved']
