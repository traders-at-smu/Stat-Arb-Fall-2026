from pathlib import Path
import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
# Locate the main project folder
PROJECT = Path(__file__).resolve().parent.parent
# Load the API keys from .env
load_dotenv(PROJECT / '.env')
KEY = os.getenv('ALPACA_API_KEY')
SECRET = os.getenv('ALPACA_SECRET_KEY')
if not KEY or not SECRET:
    raise RuntimeError('API keys missing. Check your .env file.')
# Connect ONLY to Alpaca paper trading
client = TradingClient(api_key=KEY, secret_key=SECRET, paper=True)
# Retrieve account information
account = client.get_account()
print('\n=== ALPACA PAPER TRADING ===')
print('Account status:', account.status)
print('Buying power: $', account.buying_power)
print('Account equity: $', account.equity)
print('\nSUCCESS: Connected to Alpaca!')
print('NO TRADES WERE PLACED.')
