import os
import sys
import json
import ta
from datetime import datetime
import time

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'utilities'))
from bitget_spot import BitgetSpot  # Import BitgetSpot instead of BitgetFutures

# --- CONFIG ---
params = {
    'symbol': '/USDT',  # Changed symbol format for spot trading
    'timeframe': '1h',
    'balance_fraction': 1,
    'average_type': 'DCM',  # 'SMA', 'EMA', 'WMA', 'DCM' 
    'average_period': 5,
    'stop_loss_pct': 0.4,
    'price_jump_pct': 0.3,  # Close position if price drops/rises by this percentage
}

key_path = os.path.join(os.path.dirname(__file__), '..', '..', 'secret.json')
key_name = 'envelope'

tracker_file = os.path.join(os.path.dirname(__file__), f"tracker_{params['symbol'].replace('/', '-').replace(':', '-')}.json")

trigger_price_delta = 0.005

# --- AUTHENTICATION ---
print(f"\n{datetime.now().strftime('%H:%M:%S')}: >>> starting execution for {params['symbol']}")
with open(key_path, "r") as f:
    api_setup = json.load(f)[key_name]
bitget = BitgetSpot(api_setup)  # Instantiate BitgetSpot instead of BitgetFutures

# --- TRACKER FILE ---
if not os.path.exists(tracker_file):
    with open(tracker_file, 'w') as file:
        json.dump({"status": "ok_to_trade", "last_side": None, "stop_loss_ids": []}, file)

def read_tracker_file(file_path):
    with open(file_path, 'r') as file:
        return json.load(file)

def update_tracker_file(file_path, data):
    with open(file_path, 'w') as file:
        json.dump(data, file)

# --- CANCEL OPEN ORDERS ---
orders = bitget.fetch_open_orders(params['symbol'])
for order in orders:
    bitget.cancel_order(order['id'], params['symbol'])
print(f"{datetime.now().strftime('%H:%M:%S')}: open orders cancelled")

# --- FETCH OHLCV DATA, CALCULATE INDICATORS ---
data = bitget.fetch_recent_ohlcv(params['symbol'], params['timeframe'], 100).iloc[:-1]

# Calculate ATR for volatility measure
atr_period = params['average_period']
data['ATR'] = average_true_range(data['high'], data['low'], data['close'], window=atr_period)

# Define a base envelope percentage
base_envelope_pct = 0.05  # 5% as a base

# Define minimum and maximum number of envelopes
min_envelopes = 3
max_envelopes = 10

# Determine number of envelope bands based on volatility
volatility_factor = data['ATR'].iloc[-1] / data['close'].mean()  # Relative volatility
num_envelopes = max(min_envelopes, min(int(volatility_factor * max_envelopes), max_envelopes))

# Calculate dynamic envelopes
for i in range(num_envelopes):
    data[f'band_low_{i + 1}'] = data['average'] * (1 - (base_envelope_pct * (i + 1) * data['ATR'].iloc[-1]))

print(f"{datetime.now().strftime('%H:%M:%S')}: OHLCV data and ATR fetched, dynamic envelopes calculated")

# --- CHECKS IF STOP LOSS WAS TRIGGERED ---
tracker_info = read_tracker_file(tracker_file)
closed_orders = bitget.fetch_closed_orders(params['symbol'])
if len(closed_orders) > 0 and closed_orders[-1]['id'] in tracker_info['stop_loss_ids']:
    update_tracker_file(tracker_file, {
        "last_side": closed_orders[-1]['side'],
        "status": "stop_loss_triggered",
        "stop_loss_ids": [],
    })
    print(f"{datetime.now().strftime('%H:%M:%S')}: /!\\ stop loss was triggered")

# --- OK TO TRADE CHECK ---
print(f"{datetime.now().strftime('%H:%M:%S')}: okay to trade check, status was {tracker_info['status']}")
last_price = data['close'].iloc[-1]
resume_price = data['average'].iloc[-1]
if tracker_info['status'] != "ok_to_trade":
    if ('buy' == tracker_info['last_side'] and last_price >= resume_price) or (
            'sell' == tracker_info['last_side'] and last_price <= resume_price):
        update_tracker_file(tracker_file, {"status": "ok_to_trade", "last_side": tracker_info['last_side']})
        print(f"{datetime.now().strftime('%H:%M:%S')}: status is now ok_to_trade")
    else:
        print(f"{datetime.now().strftime('%H:%M:%S')}: <<< status is still {tracker_info['status']}")
        sys.exit()

# --- PLACE ENTRY ORDERS ---
balance = params['balance_fraction'] * bitget.fetch_balance()['USDT']['total']
if 'buy' == tracker_info['last_side']:
    range_longs = range(num_envelopes - len([o for o in orders if o['side'] == 'buy']), num_envelopes)
else:
    range_longs = range(num_envelopes)

for i in range_longs:
    amount = balance / num_envelopes / data[f'band_low_{i + 1}'].iloc[-1]
    min_amount = bitget.fetch_min_amount_tradable(params['symbol'])
    if amount >= min_amount:
        bitget.place_trigger_market_order(
            symbol=params['symbol'],
            side='buy',
            amount=amount,
            trigger_price=(1 + trigger_price_delta) * data[f'band_low_{i + 1}'].iloc[-1],
            print_error=True,
        )
        print(f"{datetime.now().strftime('%H:%M:%S')}: placed open long trigger market order of {amount}, trigger price {1.005 * data[f'band_low_{i + 1}'].iloc[-1]}")
    else:
        print(f"{datetime.now().strftime('%H:%M:%S')}: /!\\ long orders not placed for envelope {i+1}, amount {amount} smaller than minimum requirement {min_amount}")

# --- MONITOR FOR OPEN POSITIONS AND PLACE TP/SL ORDERS ---
while True:
    time.sleep(60)  # Check every 60 seconds
    open_orders = bitget.fetch_open_orders(params['symbol'])
    positions = bitget.fetch_open_orders(params['symbol'])  # Re-fetch open orders to check if any have been filled
    if positions:
        position = positions[0]  # Assuming only one position at a time
        if 'buy' == position['side']:
            close_side = 'sell'
            stop_loss_price = float(position['price']) * (1 - params['stop_loss_pct'])
            take_profit_price = data['average'].iloc[-1]

            amount = position['amount']
            bitget.place_trigger_market_order(
                symbol=params['symbol'],
                side=close_side,
                amount=amount,
                trigger_price=take_profit_price,
                print_error=True,
            )
            print(f"{datetime.now().strftime('%H:%M:%S')}: placed exit long trigger market order of {amount}, trigger price {take_profit_price}")
            
            sl_order = bitget.place_trigger_market_order(
                symbol=params['symbol'],
                side=close_side,
                amount=amount,
                trigger_price=stop_loss_price,
                print_error=True,
            )
            tracker_info['stop_loss_ids'] = [sl_order['id']]
            update_tracker_file(tracker_file, tracker_info)
            print(f"{datetime.now().strftime('%H:%M:%S')}: placed stop loss trigger market order of {amount}, trigger price {stop_loss_price}")
        else:
            current_price = bitget.fetch_ticker(params['symbol'])['last']
            entry_price = float(position['price'])
            if current_price <= entry_price * (1 - params['price_jump_pct']):
                bitget.place_market_order(
                    symbol=params['symbol'],
                    side='sell',
                    amount=position['amount'],
                    print_error=True,
                )
                update_tracker_file(tracker_file, {
                    "status": "ok_to_trade",
                    "last_side": "sell",
                    "stop_loss_ids": [],
                })
                print(f"{datetime.now().strftime('%H:%M:%S')}: /!\\ price drop detected, closed long position at {current_price}")
            break
    else:
        print(f"{datetime.now().strftime('%H:%M:%S')}: No position open yet. Continuing to monitor...")
