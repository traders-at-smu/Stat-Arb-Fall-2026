# SMU TRADERS — ETF STATISTICAL ARBITRAGE
#
# Historical strategy-to-order demonstration.
#
# Features:
# - Uses Ian's original strategy calculations
# - Checks official auction-data completeness
# - Calculates ETF portfolio weights and hedges
# - Converts target positions into whole-share orders
# - Checks Alpaca asset eligibility
# - Blocks stale or ineligible portfolios
# - Exports a CSV report
#
# NO ORDER SUBMISSION IS IMPLEMENTED.
from pathlib import Path
import argparse
import os
import sys
import pandas as pd
from strategy.preview import (
    latest_complete_session,
    next_night_positions,
    compare_portfolios,
    size_orders,
    signal_age,
    asset_validation,
)
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
# ============================================================
# CONFIGURATION
# ============================================================
PROJECT = Path(__file__).resolve().parent
from strategy.config import load_settings, research_root
SETTINGS = load_settings()
IAN = research_root()
DATA = PROJECT / 'data'
BOOK_SIZE = SETTINGS['book_size']
MAX_AGE_DAYS = SETTINGS['max_signal_age_days']
# ============================================================
# COMMAND-LINE ARGUMENTS
# ============================================================
parser = argparse.ArgumentParser(description='SMU Traders Stat Arb Dry Run')
parser.add_argument(
    '--dry-run',
    action='store_true',
    help='Generate proposed orders without submitting trades'
)
parser.add_argument(
    '--preclose-snapshot',
    type=Path,
    help='Offline as-of JSON snapshot; pre-close preview only'
)
parser.add_argument(
    '--as-of-replay',
    help='Timezone-aware decision time for snapshot replay'
)
parser.add_argument(
    '--preclose-output',
    type=Path,
    default=DATA / 'preclose_report.json'
)
args = parser.parse_args()
if not args.dry_run:
    parser.error('Only --dry-run is supported.')
if args.as_of_replay and (not args.preclose_snapshot):
    parser.error('--as-of-replay requires --preclose-snapshot')
if args.preclose_snapshot:
    # Avoid numba cache writes in Ian's research tree. No dotenv or
    # broker calls.
    os.environ['NUMBA_DISABLE_JIT'] = '1'
sys.path.insert(0, str(IAN))
import forward_test as FT
if args.preclose_snapshot:
    from strategy.preclose import run_snapshot
    run_snapshot(
        FT,
        args.preclose_snapshot,
        args.preclose_output,
        args.as_of_replay
    )
    raise SystemExit(0)
load_dotenv(PROJECT / '.env')
# ============================================================
# START
# ============================================================
print('\n' + '=' * 48)
print('SMU TRADERS — STATISTICAL ARBITRAGE')
print('MODE: DRY RUN')
print('=' * 48)
# ============================================================
# STEP 1 — LOAD IAN'S ORIGINAL STRATEGY
# ============================================================
print("\n[1] Loading Ian's strategy...")
(P, R_on, R_id, bon, bid, e_on, e_id, U, auc) = FT.build_history()
(W, ok) = FT.weights(e_on, U['all31'])
print('Historical trading dates:', len(W))
print('ETF universe:', len(U['all31']))
# ============================================================
# STEP 2 — FIND THE LATEST COMPLETE TRADING SESSION
# ============================================================
print('\n[2] Checking auction-data completeness...')
# Read the ORIGINAL auction records, not the processed
# price panels, which may contain filled-forward values.
auction_file = IAN / 'results' / 'auctions.parquet'
raw_auctions = pd.read_parquet(auction_file)
signal_date = latest_complete_session(raw_auctions, W.index, ok, FT.PC.SYMS)
print('Latest available date:', W.index[-1].date())
print('Latest verified complete session:', signal_date.date())
if signal_date != W.index[-1]:
    print(
        (
            'WARNING: Latest session has missing auction data. '
            'Using the last verified complete session.'
        )
    )
# ============================================================
# STEP 3 — BUILD THE HEDGED PORTFOLIO
# ============================================================
print('\n[3] Building portfolio...')
# Re-estimate the next-night state using observations only through signal_date.
# Signals remain unchanged; their full-sample calibration is a research
# limitation.
positions = next_night_positions(FT, W, R_on, signal_date, U['all31'])
prices = P['close'].loc[signal_date].reindex(positions.index)
orders = size_orders(positions, prices, BOOK_SIZE)
gross_exposure = float(positions.abs().sum() * BOOK_SIZE)
print('Original ETF book: $', f'{BOOK_SIZE:,.2f}')
print('Gross exposure: $', f'{gross_exposure:,.2f}')
# ============================================================
# STEP 4 — CONVERT PORTFOLIO INTO SHARE QUANTITIES
# ============================================================
print('\n[4] Generating proposed orders...')
print('Proposed orders:', len(orders))
# ============================================================
# STEP 5 — CHECK SIGNAL FRESHNESS
# ============================================================
print('\n[5] Checking data freshness...')
today = pd.Timestamp.now().normalize()
(age, stale) = signal_age(signal_date, today, MAX_AGE_DAYS)
print('Signal date:', signal_date.date())
print('Calendar days since signal:', age)
print('Stale:', stale)
# A recent date is not proof that the signal is
# correctly timed for a closing-auction submission.
# ============================================================
# STEP 6 — CONNECT TO ALPACA PAPER TRADING
# ============================================================
print('\n[6] Connecting to Alpaca paper trading...')
key = os.getenv('ALPACA_API_KEY')
secret = os.getenv('ALPACA_SECRET_KEY') or os.getenv('ALPACA_API_SECRET')
if not key or not secret:
    raise RuntimeError('Missing Alpaca credentials in .env.')
client = TradingClient(api_key=key, secret_key=secret, paper=True)
account = client.get_account()
print('Account status:', account.status)
print('Account equity: $', account.equity)
print('Buying power: $', account.buying_power)
# ============================================================
# STEP 7 — CHECK ORDER ELIGIBILITY
# ============================================================
print('\n[7] Checking Alpaca asset eligibility...')
validation = asset_validation(orders, client.get_asset)
order_requests = [
    MarketOrderRequest(symbol=str(r.symbol), qty=int(r.quantity),
        side=OrderSide.BUY if r.side == 'BUY' else OrderSide.SELL,
            time_in_force=TimeInForce.CLS) for r in orders.itertuples(
                index=False)
]
# ============================================================
# STEP 8 — MERGE VALIDATION RESULTS
# ============================================================
results = orders.merge(validation, on='symbol', validate='one_to_one')
failed = results[~results['eligible']].copy()
print('Eligible orders:', int(results['eligible'].sum()))
print('Ineligible orders:', len(failed))
if not failed.empty:
    print('\nRestricted positions:')
    print(
        failed[['symbol', 'side', 'quantity', 'borrow_status',
            'reason']].to_string(index=False)
    )
# ============================================================
# STEP 9 — PORTFOLIO SAFETY GATE
# ============================================================
print('\n[8] Running portfolio safety checks...')
basic_checks_passed = not stale and failed.empty and (len(order_requests) > 0)
if stale:
    print('BLOCKED: Signal is stale.')
if not failed.empty:
    print('BLOCKED: Required orders are ineligible.')
if basic_checks_passed:
    print('Basic validation checks passed.')
    print('Historical preview only: NOT cleared for automated execution.')
else:
    print('PORTFOLIO BLOCKED.')
# Execution is always disabled, regardless of result.
execution_enabled = False
# ============================================================
# STEP 10 — EXPORT RESULTS
# ============================================================
print('\n[9] Saving report...')
DATA.mkdir(parents=True, exist_ok=True)
results['signal_date'] = str(signal_date.date())
results['signal_age_days'] = age
results['stale'] = stale
results['basic_checks_passed'] = basic_checks_passed
results['execution_enabled'] = execution_enabled
results['mode'] = 'HISTORICAL_DRY_RUN'
results['hedge_timing'] = 'POSTERIOR_THROUGH_SIGNAL_SESSION'
results['hedge_calibration'] = 'PREFIX_ONLY'
results['signal_calibration'] = 'ORIGINAL_FULL_HISTORY_RESEARCH_LIMITATION'
# Preserve the existing report and immutable regression fixture.
output = DATA / 'dry_run_report_corrected.csv'
baseline_file = PROJECT / 'tests' / 'fixtures' / \
    'dry_run_report_before_refactor.csv'
if baseline_file.exists():
    baseline = pd.read_csv(baseline_file)
    if baseline['signal_date'].eq(str(signal_date.date())).all():
        comparison = compare_portfolios(orders, baseline)
        comparison.to_csv(
            DATA / 'dry_run_portfolio_comparison.csv',
            index=False
        )
        changed = comparison[
            (comparison['weight_change'].abs() > 1e-12) | (comparison[
                'signed_share_change'] != 0)
        ]
        print('\nCorrected minus original portfolio (signed shares):')
        print(changed.to_string(index=False))
    else:
        print('Baseline session differs; skipping portfolio comparison.')
print(
    (
        'Hedge timing: posterior through signal session; '
        'prefix-only calibration.'
    )
)
print(
    (
        'Research limitation: original signals still use '
        'full-history calibration.'
    )
)
results.to_csv(output, index=False)
print('Report saved:', output)
# ============================================================
# FINAL SUMMARY
# ============================================================
print('\n' + '=' * 48)
print('FINAL SUMMARY')
print('=' * 48)
print('Signal date:', signal_date.date())
print('ETF universe:', len(U['all31']))
print('Proposed orders:', len(order_requests))
print('Eligible:', int(results['eligible'].sum()))
print('Ineligible:', len(failed))
print('Gross exposure: $', f'{gross_exposure:,.2f}')
print('Basic checks passed:', basic_checks_passed)
print('Execution enabled:', execution_enabled)
print('\nNO ORDERS SUBMITTED.')
print('=' * 48)
