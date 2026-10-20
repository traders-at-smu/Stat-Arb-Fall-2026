from pathlib import Path
from .config import load_settings
import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / '.env')
client = TradingClient(
    os.environ['ALPACA_API_KEY'],
    os.environ['ALPACA_SECRET_KEY'],
    paper=True
)
symbols = load_settings()['shortability_symbols']
print('\n=== ALPACA SHORTABILITY CHECK ===')
for symbol in symbols:
    asset = client.get_asset(symbol)
    print(f'\n{symbol}')
    print('Tradable:', asset.tradable)
    print('Shortable:', asset.shortable)
    print('Easy to borrow:', asset.easy_to_borrow)
    print('Status:', asset.status)
print('\nNO ORDERS SUBMITTED.')
