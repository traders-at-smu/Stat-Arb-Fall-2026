# Offline regression checks against Ian's unchanged files and stored history.
#
# Disable JIT before importing Ian: numba's cache must never write in
# ian_original.
# No data-update, report-writing, credential-loading or broker functions are
# called.
import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from strategy.preview import (
    historical_positions,
    size_orders,
    latest_complete_session,
    next_night_betas,
    next_night_positions,
    compare_portfolios,
)
ROOT = Path(__file__).resolve().parents[1]
from strategy.config import research_root
RESEARCH = research_root()
REQUIRED_PRIVATE_INPUTS = [
    RESEARCH / 'forward_test.py',
    RESEARCH / 'results/auctions.parquet',
    ROOT / 'tests/fixtures/dry_run_report_before_refactor.csv',
    ROOT / 'tests/fixtures/october15_corrected_portfolio.csv',
    ROOT / 'data/dry_run_report.csv'
]
pytestmark = [
    pytest.mark.research,
    pytest.mark.skipif(not all((path.is_file(
        ) for path in REQUIRED_PRIVATE_INPUTS)), reason=(
            'Private research/history/regression inputs '
            'intentionally excluded from Git'
        ))
]


@pytest.fixture(scope='module')
def history():
    os.environ['NUMBA_DISABLE_JIT'] = '1'
    sys.path.insert(0, str(RESEARCH))
    import forward_test as ft
    data = ft.build_history()
    (weights, ok) = ft.weights(data[5], data[7]['all31'])
    return (ft, data, weights, ok)


def test_preserved_report_portfolio_and_share_parity(history):
    (ft, data, weights, ok) = history
    (prices, _, _, bon, _, _, _, universes, _) = data
    saved = pd.read_csv(
        ROOT / 'tests/fixtures/dry_run_report_before_refactor.csv'
    )
    date = pd.Timestamp(saved.signal_date.iloc[0])
    positions = historical_positions(
        ft,
        weights,
        bon,
        date,
        universes['all31']
    )
    generated = size_orders(positions, prices['close'].loc[date], 100000)
    columns = [
        'symbol',
        'weight',
        'target_dollars',
        'reference_price',
        'signed_shares',
        'side',
        'quantity'
    ]
    pd.testing.assert_frame_equal(
        generated[columns].reset_index(drop=True),
        saved[columns].reset_index(drop=True),
        check_dtype=False,
        rtol=1e-12,
        atol=1e-10
    )
    raw = pd.read_parquet(RESEARCH / 'results/auctions.parquet')
    assert latest_complete_session(raw, weights.index, ok, ft.PC.SYMS) == date


def test_every_backtest_portfolio_and_exact_timing_difference(
    history,
    monkeypatch
):
    (ft, data, weights, ok) = history
    (prices, _, _, bon, bid, e_on, e_id, universes, costs) = data
    universe = universes['all31']
    original = ft.netted
    actual = []

    def capture(w, b1, b2, u):
        result = original(w, b1, b2, u)
        actual.append((w.name, result.copy()))
        return result
    monkeypatch.setattr(ft, 'netted', capture)
    (
        ledger,
        backtest_weights
    ) = ft.nightly(
        prices,
        bon,
        bid,
        e_on,
        e_id,
        universes,
        costs,
        'all31',
        'night'
    )
    monkeypatch.setattr(ft, 'netted', original)
    pd.testing.assert_frame_equal(backtest_weights, weights)
    differences = []
    for (date, backtest) in actual:
        next_date = weights.index[weights.index.get_loc(date) + 1]
        expected = original(
            weights.loc[date],
            bon[0].loc[next_date],
            bon[1].loc[next_date],
            universe
        )
        pd.testing.assert_series_equal(backtest, expected)
        preview = historical_positions(ft, weights, bon, date, universe)
        # Independently reconstruct the hedge difference from child
        # weights and beta deltas.
        delta = pd.Series(0.0, index=ft.PC.SYMS)
        for symbol in universe:
            w = weights.loc[date, symbol]
            delta[
                'SPY'
            ] -= w * (
                bon[0].loc[next_date, symbol] - bon[0].loc[date, symbol]
            )
            parent = ft.PC.PARENT[symbol]
            if parent != 'SPY':
                delta[
                    parent
                ] -= w * (
                    bon[1].loc[next_date, symbol] - bon[1].loc[date, symbol]
                )
        np.testing.assert_allclose(
            (backtest - preview).values,
            delta.values,
            atol=1e-14
        )
        differences.append((date, float(delta.abs().max())))
    assert len(actual) == len(ledger) > 1000
    assert any((value > 1e-08 for (_, value) in differences))
    (date, maximum) = max(differences, key=lambda row: row[1])
    sample = pd.Timestamp('2026-10-15')
    next_sample = weights.index[weights.index.get_loc(sample) + 1]
    a = historical_positions(ft, weights, bon, sample, universe)
    b = original(
        weights.loc[sample],
        bon[0].loc[next_sample],
        bon[1].loc[next_sample],
        universe
    )
    print(
        '\nOctober 15 backtest minus preview netted weights:',
        {s: round(float(v), 10) for (s, v) in (b - a).items() if abs(
            v) > 1e-12}
    )
    print(
        f'\nCompared ' \
            f'{len(actual)}' \
            f' historical portfolios; maximu' \
            f'm hedge-weight difference ' \
            f'{maximum:.10f}' \
            f' on ' \
            f'{date.date()}' \
            f'. Preview uses beta[t]; backte' \
            f'st uses beta[next session].'
    )


def test_filter_emits_prior_before_observation(history):
    (ft, *_) = history
    # Fixed noise scales isolate state timing from full-sample scale
    # calibration.
    y = np.array([0.01, 0.02, 0.03])
    x = np.array([0.01, 0.01, 0.01])
    before = ft.PC.A._kalman2(y, x, 1e-07, 0.001, 0.0001)
    changed = y.copy()
    changed[1] = -0.1
    after = ft.PC.A._kalman2(changed, x, 1e-07, 0.001, 0.0001)
    np.testing.assert_array_equal(before[:2], after[:2])
    assert before[2] != after[2]
    (b1, b2) = ft.PC.A._kalman3(y, x, x * 2, 1e-07, 0.001, 0.001, 0.0001)
    (c1, c2) = ft.PC.A._kalman3(changed, x, x * 2, 1e-07, 0.001, 0.001, 0.0001)
    np.testing.assert_array_equal(b1[:2], c1[:2])
    np.testing.assert_array_equal(b2[:2], c2[:2])
    assert b1[2] != c1[2] and b2[2] != c2[2]


def test_main_dry_run_offline_preserves_report(history, monkeypatch):
    import runpy
    from types import SimpleNamespace
    import dotenv
    import alpaca.trading.client
    (ft, data, weights, _) = history
    path = ROOT / 'data/dry_run_report.csv'
    baseline = path.read_bytes()
    saved = pd.read_csv(
        ROOT / 'tests/fixtures/dry_run_report_before_refactor.csv'
    )
    assets = saved.set_index('symbol')

    class FakeBroker:

        def __init__(self, *args, **kwargs):
            assert kwargs['paper'] is True

        def get_account(self):
            return (
                SimpleNamespace(status='TEST', equity='100000',
                    buying_power='100000')
            )

        def get_asset(self, symbol):
            row = assets.loc[symbol]
            return (
                SimpleNamespace(tradable=row.tradable, shortable=row.shortable,
                    borrow_status=row.borrow_status)
            )

        def submit_order(self, *args, **kwargs):
            pytest.fail('Orders must never be submitted')
    monkeypatch.setattr(alpaca.trading.client, 'TradingClient', FakeBroker)
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *a, **k: False)
    monkeypatch.setenv('ALPACA_API_KEY', 'offline-test-placeholder')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'offline-test-placeholder')
    monkeypatch.setattr(ft, 'build_history', lambda : data)
    monkeypatch.setattr(sys, 'argv', ['main.py', '--dry-run'])
    outputs = []
    monkeypatch.setattr(
        pd.DataFrame,
        'to_csv',
        lambda self,
        *a,
        **k: outputs.append(self.copy())
    )
    runpy.run_path(str(ROOT / 'main.py'), run_name='__main__')
    # comparison and corrected report; original untouched
    assert len(outputs) == 2
    (comparison, result) = outputs
    expected_positions = next_night_positions(
        ft,
        weights,
        data[1],
        pd.Timestamp(saved.signal_date.iloc[0]),
        data[7]['all31']
    )
    expected_orders = size_orders(
        expected_positions,
        data[0]['close'].loc[pd.Timestamp(saved.signal_date.iloc[0])],
        100000
    )
    columns = [
        'symbol',
        'weight',
        'target_dollars',
        'reference_price',
        'signed_shares',
        'side',
        'quantity'
    ]
    pd.testing.assert_frame_equal(
        result[columns].reset_index(drop=True),
        expected_orders[columns].reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(
        comparison,
        compare_portfolios(expected_orders, saved)
    )
    # Non-portfolio eligibility fields are unchanged under the fake broker.
    cols = [
        'symbol',
        'eligible',
        'tradable',
        'shortable',
        'borrow_status',
        'reason',
        'signal_date',
        'mode'
    ]
    pd.testing.assert_frame_equal(
        result[cols].reset_index(drop=True),
        saved[cols].reset_index(drop=True),
        check_dtype=False
    )
    assert not result.execution_enabled.any()
    assert path.read_bytes() == baseline
SAMPLE_DATES = [
    '2017-06-01',
    '2020-03-16',
    '2023-08-01',
    '2026-09-22',
    '2026-10-15'
]


@pytest.mark.parametrize('date', SAMPLE_DATES)
def test_next_night_beta_and_portfolio_parity_with_shared_calibration(
    history,
    monkeypatch,
    date
):
    # Exact original-backtest parity isolates timing, NOT causal calibration.
    (ft, data, weights, _) = history
    (returns, bon, universe) = (data[1], data[3], data[7]['all31'])
    i = returns.index.get_loc(pd.Timestamp(date))
    scales = []
    for child in ft.PC.ALL:
        xs = [returns.SPY.to_numpy()]
        parent = ft.PC.PARENT[child]
        if parent != 'SPY':
            xs.append(returns[parent].to_numpy())
        scales.append(ft.PC.A._noise_scales(returns[child].to_numpy(), xs))
    scale_iter = iter(scales)
    monkeypatch.setattr(
        ft.PC.A,
        '_noise_scales',
        lambda *args: next(scale_iter)
    )
    (b1, b2) = next_night_betas(ft, returns, date)
    np.testing.assert_allclose(b1, bon[0].iloc[i + 1], atol=1e-14, rtol=0)
    np.testing.assert_allclose(b2, bon[1].iloc[i + 1], atol=1e-14, rtol=0)
    expected = ft.netted(
        weights.loc[date],
        bon[0].iloc[i + 1],
        bon[1].iloc[i + 1],
        universe
    )
    actual = ft.netted(weights.loc[date], b1, b2, universe)
    pd.testing.assert_series_equal(actual, expected, atol=1e-14, rtol=0)


@pytest.mark.parametrize('date', SAMPLE_DATES)
def test_asof_calibration_portfolio_matches_ian_backtest_construction(
    history,
    monkeypatch,
    date
):
    # Ian's next-row construction with calibration restricted to the same
    # prefix.
    (ft, data, weights, _) = history
    (returns, universe) = (data[1], data[7]['all31'])
    i = returns.index.get_loc(pd.Timestamp(date))
    noise_scales = ft.PC.A._noise_scales
    monkeypatch.setattr(
        ft.PC.A,
        '_noise_scales',
        lambda y,
        xs: noise_scales(y[:i + 1], [x[:i + 1] for x in xs])
    )
    reference = ft.PC.seg_betas(returns)
    expected = ft.netted(
        weights.loc[date],
        reference[0].iloc[i + 1],
        reference[1].iloc[i + 1],
        universe
    )
    monkeypatch.setattr(ft.PC.A, '_noise_scales', noise_scales)
    actual = next_night_positions(ft, weights, returns, date, universe)
    pd.testing.assert_series_equal(actual, expected, atol=1e-14, rtol=0)


def test_next_night_future_independence_and_latest_row(history):
    (ft, data, weights, _) = history
    returns = data[1]
    date = pd.Timestamp('2026-10-15')
    before = next_night_betas(ft, returns, date)
    altered = returns.copy()
    altered.loc[altered.index > date] = 12345.0
    for panel in [returns.loc[:date], altered]:
        after = next_night_betas(ft, panel, date)
        for (a, b) in zip(before, after):
            pd.testing.assert_series_equal(a, b)
    # Prefix-only scales actually differ from the full-history original.
    i = returns.index.get_loc(date)
    assert np.max(
        np.abs(before[0].values - data[3][0].iloc[i + 1].values)
    ) > 1e-08


def test_missing_observation_skips_update_and_sentinel_is_not_calibrated(
    history,
    monkeypatch
):
    (ft, data, *_) = history
    returns = data[1].loc[:'2026-10-15'].copy()
    returns.iloc[-1] = np.nan
    original = returns.copy(deep=True)
    # An all-missing final observation cannot update any coefficient.
    before = next_night_betas(ft, returns.iloc[:-1], returns.index[-2])
    after = next_night_betas(ft, returns, returns.index[-1])
    for (a, b) in zip(before, after):
        np.testing.assert_array_equal(a.values, b.values)
    pd.testing.assert_frame_equal(returns, original)


def test_corrected_october_15_regression(history):
    (ft, data, weights, _) = history
    saved = pd.read_csv(
        ROOT / 'tests/fixtures/dry_run_report_before_refactor.csv'
    )
    positions = next_night_positions(
        ft,
        weights,
        data[1],
        '2026-10-15',
        data[7]['all31']
    )
    orders = size_orders(positions, data[0]['close'].loc['2026-10-15'], 100000)
    pinned = pd.read_csv(
        ROOT / 'tests/fixtures/october15_corrected_portfolio.csv'
    )
    pd.testing.assert_frame_equal(
        orders.reset_index(drop=True),
        pinned.reset_index(drop=True),
        check_dtype=False,
        rtol=1e-12,
        atol=1e-10
    )
    comparison = compare_portfolios(orders, saved)
    assert comparison.signed_share_change.ne(0).any()
    # Pure sleeve positions stay unchanged; only SPY/sector hedge legs change.
    sleeves = comparison[comparison.symbol.isin(ft.PC.SLV)]
    np.testing.assert_allclose(sleeves.weight_change, 0, atol=1e-14)
    assert sleeves.signed_share_change.eq(0).all()
    print(
        '\nCorrected October 15 portfolio:\n' + comparison[
            comparison.weight_change.abs().gt(
                1e-12) | comparison.signed_share_change.ne(0)].to_string(
                    index=False)
    )


def test_adapter_passes_only_prefix_and_missing_sentinel(history, monkeypatch):
    (ft, data, *_) = history
    original = ft.PC.seg_betas
    date = pd.Timestamp('2026-10-15')
    prefix = data[1].loc[:date]
    calls = []

    def inspect(panel):
        pd.testing.assert_frame_equal(panel.iloc[:-1], prefix)
        assert panel.iloc[-1].isna().all()
        assert len(panel) == len(prefix) + 1
        calls.append(True)
        return original(panel)
    monkeypatch.setattr(ft.PC, 'seg_betas', inspect)
    next_night_betas(ft, data[1], date)
    assert calls == [True]


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'unsorted'])
def test_adapter_rejects_ambiguous_dates(history, problem):
    (ft, data, *_) = history
    panel = data[1]
    date = pd.Timestamp('2026-10-15')
    if problem == 'missing':
        panel = panel.drop(date)
    elif problem == 'duplicate':
        panel = pd.concat([panel, panel.iloc[[-1]]])
    else:
        panel = panel.iloc[::-1]
    with pytest.raises(ValueError):
        next_night_betas(ft, panel, date)


def test_preclose_residual_signal_and_hedge_pipeline_with_ian_math(history):
    # Numerical integration only: synthetic prices/timestamps are not
    # provenance evidence.
    from strategy.preclose import build_preclose_preview
    (ft, *_) = history
    days = pd.bdate_range(end='2026-10-19', periods=65)
    morning = '2026-10-19T10:00:00-04:00'
    (auctions, calendar) = ([], [])
    op = pd.DataFrame(index=days, columns=ft.PC.SYMS, dtype=float)
    cl = op.copy()
    for (i, d) in enumerate(days):
        date = str(d.date())
        calendar.append(
            dict(session=date, open=date + 'T09:30:00-04:00',
                close=date + 'T16:00:00-04:00',
                    moc_deadline=date + 'T15:50:00-04:00')
        )
        for (j, symbol) in enumerate(ft.PC.SYMS):
            opening = 100 + j + i * 0.1 + np.sin(i * 0.7 + j)
            closing = opening * (1 + 0.002 * np.cos(i + j * 0.3))
            (op.loc[d, symbol], cl.loc[d, symbol]) = (opening, closing)
            kinds = [
                'close'
            ] if i == 0 else [
                'open'
            ] if i == len(
                days
            ) - 1 else [
                'open',
                'close'
            ]
            for kind in kinds:
                stamp = date + (
                    'T09:30:00-04:00' if kind == 'open' else 'T16:00:00-04:00'
                )
                auctions.append(
                    dict(session=date, symbol=symbol, kind=kind,
                        price=opening if kind == 'open' else closing,
                            factor=1.0, observed_at=stamp, available_at=stamp,
                                factor_available_at=stamp)
                )
    prices = [
        dict(symbol=s, price=120.0, observed_at='2026-10-19T15:29:45-04:00',
            available_at='2026-10-19T15:29:50-04:00') for s in ft.PC.SYMS
    ]
    snapshot = dict(
        session='2026-10-19',
        history_start=str(days[0].date()),
        morning_asof=morning,
        calendar=dict(coverage_start=str(days[0].date()),
            coverage_end='2026-10-19', source='synthetic numerical fixture',
                available_at='2026-01-01T00:00:00-05:00', sessions=calendar),
        auctions=auctions,
        prices=prices
    )
    result = build_preclose_preview(ft, snapshot, '2026-10-19T15:30:00-04:00')
    assert result['data_checks_passed'], result['blockers']
    returns = op / cl.shift(1) - 1
    betas = ft.PC.seg_betas(returns)
    residuals = ft.PC.resid(
        returns[ft.PC.ALL],
        ft.PC.spy_panel(returns),
        ft.PC.parent_panel(returns),
        *betas
    )
    (weights, ok) = ft.weights(residuals, ft.PC.ALL)
    assert ok.iloc[-1]
    expected = next_night_positions(ft, weights, returns, days[-1], ft.PC.ALL)
    expected_orders = size_orders(
        expected,
        pd.Series(120.0, index=ft.PC.SYMS),
        100000
    )
    actual = pd.DataFrame(result['orders'])[expected_orders.columns]
    pd.testing.assert_frame_equal(
        actual.reset_index(drop=True),
        expected_orders.reset_index(drop=True)
    )
    assert result[
        'execution_approved'
    ] is False and result[
        'execution_enabled'
    ] is False
