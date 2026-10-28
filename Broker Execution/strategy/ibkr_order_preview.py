# Offline meeting demo: unchanged Ian calculations -> broker-neutral targets ->
# IBKR MOC/MOO objects and HYPOTHETICAL fills. No connection/client/order
# submission.
# Historical close prices are demonstration references, never current sizing
# data.
# Demo permissions cannot verify borrow; it is always UNKNOWN in this
# comparison.
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import pandas as pd
from ibapi.contract import Contract
from ibapi.order import Order
from .preview import (
    latest_complete_session,
    next_night_positions,
    size_orders,
    validate_orders,
)
from .ibkr_shortability import alpaca_snapshot
from .collect_preclose import SYMBOLS
from .config import load_settings, research_root
ROOT = Path(__file__).resolve().parents[1]


def load_contracts(path=None, directory=ROOT / 'data'):
    if path is None:
        paths = list(Path(directory).glob('ibkr_shortability_*.csv'))
        if not paths:
            raise ValueError(
                'No saved IBKR contract diagnostic; supply --contracts'
            )
        path = max(paths, key=lambda p: p.stat().st_mtime_ns)
    path = Path(path)
    df = pd.read_csv(path)
    if df.symbol.duplicated().any() or set(df.symbol) != set(SYMBOLS):
        raise ValueError('Require exactly the 32 saved ETF contracts')
    if (
        not df.contract_status.eq('VERIFIED_US_ETF').all()
        or not df.stock_type.eq('ETF').all()
    ):
        raise ValueError('Contract/ETF verification incomplete')
    ids = pd.to_numeric(df.con_id, errors='coerce')
    if (
        not (ids.notna() & (ids > 0) & (ids % 1 == 0)).all()
        or ids.duplicated().any()
        or df.primary_exchange.isna().any()
    ):
        raise ValueError('Invalid/duplicate IBKR contract identifiers')
    return (df.set_index('symbol'), path)


def auction_preview(symbol, side, quantity, record, phase):
    validate_orders(
        pd.DataFrame([dict(symbol=symbol, side=side, quantity=quantity)])
    )
    if phase not in {'entry', 'exit'}:
        raise ValueError('Invalid preview phase')
    c = Contract()
    c.conId = int(record.con_id)
    c.symbol = symbol
    c.secType = 'STK'
    c.currency = 'USD'
    c.exchange = 'SMART'
    c.primaryExchange = record.primary_exchange
    o = Order()
    o.action = side
    o.totalQuantity = int(quantity)
    o.orderType = 'MOC' if phase == 'entry' else 'MKT'
    o.tif = 'DAY' if phase == 'entry' else 'OPG'
    o.transmit = False
    o.outsideRth = False
    o.whatIf = False
    return (c, o)


def simulate(orders, filled=None):
    # Pure illustration: no accepted/filled broker records or execution ledger.
    validate_orders(orders)
    result = orders[['symbol', 'side', 'quantity']].copy()
    if filled is None:
        filled = dict(zip(result.symbol, result.quantity))
        partial = result[result.quantity >= 2]
        if partial.empty:
            raise ValueError(
                'No quantity large enough for a partial-fill illustration'
            )
        filled[partial.symbol.iloc[0]] = int(partial.quantity.iloc[0]) // 2
        unfilled = next(
            (s for s in result.symbol if s != partial.symbol.iloc[0])
        )
        filled[unfilled] = 0
    if set(filled) != set(result.symbol):
        raise ValueError(
            'Hypothetical fills must cover exactly the proposed symbols'
        )
    for r in result.itertuples():
        q = filled[r.symbol]
        if (
            isinstance(q, bool)
            or not isinstance(q, (int, float))
            or (not 0 <= q <= r.quantity)
            or q % 1
        ):
            raise ValueError('Invalid hypothetical filled quantity')
    result['simulated_filled_qty'] = result.symbol.map(filled).astype(int)
    result[
        'simulated_remaining_qty'
    ] = result.quantity - result.simulated_filled_qty
    result['exit_side'] = result.side.map({'BUY': 'SELL', 'SELL': 'BUY'})
    result['exit_quantity'] = result.simulated_filled_qty
    result['fill_source'] = 'HYPOTHETICAL_FINAL_FILL_SCENARIO'
    result['execution_enabled'] = False
    return result


def build_previews(orders, contracts, fills):
    rows = []
    for phase in ['entry', 'exit']:
        for r in fills.itertuples():
            qty = r.quantity if phase == 'entry' else r.exit_quantity
            if not qty:
                continue
            side = r.side if phase == 'entry' else r.exit_side
            (
                c,
                o
            ) = auction_preview(
                r.symbol,
                side,
                qty,
                contracts.loc[r.symbol],
                phase
            )
            rows.append(
                dict(phase=phase, symbol=c.symbol, con_id=c.conId,
                    exchange=c.exchange, primary_exchange=c.primaryExchange,
                        currency=c.currency, sec_type=c.secType,
                            action=o.action, quantity=o.totalQuantity,
                                order_type=o.orderType, tif=o.tif,
                                    transmit=o.transmit,
                                        execution_enabled=False,
                    mode='OFFLINE_HYPOTHETICAL_PREVIEW')
            )
    return pd.DataFrame(rows)


def historical_portfolio(date=None):
    # Disable numba cache writes in the preserved research tree.
    os.environ['NUMBA_DISABLE_JIT'] = '1'
    ian = research_root()
    sys.path.insert(0, str(ian))
    import forward_test as ft
    (prices, returns, _, _, _, residuals, _, universes, _) = ft.build_history()
    (weights, ok) = ft.weights(residuals, universes['all31'])
    raw = pd.read_parquet(ian / 'results' / 'auctions.parquet')
    dates = weights.index if date is None else pd.DatetimeIndex(
        [pd.Timestamp(date)]
    )
    day = latest_complete_session(raw, dates, ok, ft.PC.SYMS)
    following = weights.index[weights.index > day]
    if following.empty:
        raise ValueError(
            (
                'No following historical session available; choose an '
                'earlier date'
            )
        )
    positions = next_night_positions(
        ft,
        weights,
        returns,
        day,
        universes['all31']
    )
    return (
        (size_orders(positions, prices['close'].loc[day], load_settings()[
            'book_size']), day, following[0])
    )


def comparison(orders, contracts, alpaca_path=None):
    saved, provenance = alpaca_snapshot(alpaca_path)
    rows = []
    proposed = orders.set_index('symbol')
    for symbol in SYMBOLS:
        a = saved.get(symbol, {})
        rows.append(dict(
            symbol=symbol,
            con_id=int(contracts.loc[symbol].con_id),
            contract_status=contracts.loc[symbol].contract_status,
            proposed_signed_shares=(
                int(proposed.loc[symbol].signed_shares)
                if symbol in proposed.index else 0
            ),
            ibkr_borrow_availability='UNKNOWN',
            ibkr_shortable_shares='UNKNOWN',
            ibkr_borrow_fee='UNKNOWN',
            ibkr_raw_diagnostic_status=(
                contracts.loc[symbol].availability_status
            ),
            ibkr_diagnostic_received_at=contracts.loc[symbol].get(
                'scan_finished_at', ''
            ),
            alpaca_shortable=a.get('shortable', 'UNKNOWN'),
            alpaca_order_eligible=a.get('eligible', a.get('valid', 'UNKNOWN')),
            alpaca_signal_date=a.get('signal_date', ''),
            alpaca_eligibility_observed_at=a.get('observed_at', ''),
            alpaca_file_saved_at=provenance['file_saved_at'],
            alpaca_source=provenance['source'],
            alpaca_timestamp_note=provenance['note'],
            execution_enabled=False,
        ))
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(
        description='Offline IBKR order preview; no submission'
    )
    parser.add_argument(
        '--signal-date',
        help=(
            'Historical replay date; defaults to latest complete '
            'stored session'
        )
    )
    parser.add_argument('--contracts', type=Path)
    parser.add_argument('--alpaca-snapshot', type=Path)
    parser.add_argument(
        '--output-dir',
        type=Path,
        default=ROOT / 'data' / ('meeting_demo_' + datetime.now(
            timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    )
    args = parser.parse_args()
    (contracts, source) = load_contracts(args.contracts)
    (orders, day, next_day) = historical_portfolio(args.signal_date)
    fills = simulate(orders)
    previews = build_previews(orders, contracts, fills)
    brokers = comparison(orders, contracts, args.alpaca_snapshot)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for (
        name,
        frame
    ) in [
        ('portfolio', orders),
        ('ibkr_order_previews', previews),
        ('hypothetical_fills', fills),
        ('broker_comparison', brokers)
    ]:
        frame.to_csv(args.output_dir / (name + '.csv'), index=False)
    summary = dict(
        mode='HISTORICAL_OFFLINE_DEMO',
        signal_date=str(day.date()),
        entry_close=str(day.date()),
        exit_open=str(next_day.date()),
        contract_source=str(source),
        contract_count=len(contracts),
        entry_previews=int(previews.phase.eq('entry').sum()),
        exit_previews=int(previews.phase.eq('exit').sum()),
        short_availability='UNKNOWN',
        execution_enabled=False,
        broker_orders_submitted=0,
        limitations=[
            'Historical close reference prices; not current-session sizing.', (
                'Original signal full-history calibration and '
                'adjustment provenance remain research limitations.'
            ), (
                'Fills and unfilled remainders are hypothetical final '
                'outcomes, not broker acknowledgments.'
            ), (
                'Contract resolution does not establish borrow, routing '
                'support or execution eligibility.'
            )]
    )
    (
        args.output_dir / 'summary.json'
    ).write_text(
        json.dumps(summary, indent=2)
    )
    print(json.dumps(summary, indent=2))
    print(
        fills[['symbol', 'quantity', 'simulated_filled_qty', 'exit_side',
            'exit_quantity']].to_string(index=False)
    )
    print('Saved demonstration: ' + str(args.output_dir))
    print('OFFLINE ONLY: no TWS connection, no orders submitted or staged.')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
