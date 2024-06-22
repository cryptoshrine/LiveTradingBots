import os
import sys
import json
import ta
from datetime import datetime

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from utilities.bitget_spot import BitgetSpot  # Import BitgetSpot instead of BitgetFutures

# --- CONFIG ---
params = {
    'symbol': 'BTC/USDT',  # Changed symbol format for spot trading
    'timeframe': '1h',
    'balance_fraction': 1,
    'average_type': 'DCM',  # 'SMA', 'EMA', 'WMA', 'DCM' 
    'average_period': 5,
    'envelopes': [0.07, 0.11, 0.14],
    'stop_loss_pct': 0.4,
#    'price_jump_pct': 0.3,  # optional, remove if not wanted
}

key_path = 'LiveTradingBots/secret.json'
key_name = 'envelope'

tracker_file = f"LiveTradingBots/code/strategies/envelope/tracker_{params['symbol'].replace('/', '-').replace(':', '-')}.json"

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
if 'DCM' == params['average_type']:
    ta_obj = ta.volatility.DonchianChannel(data['high'], data['low'], data['close'], window=params['average_period'])
    data['average'] = ta_obj.donchian_channel_mband()
elif 'SMA' == params['average_type']:
    data['average'] = ta.trend.sma_indicator(data['close'], window=params['average_period'])
elif 'EMA' == params['average_type']:
    data['average'] = ta.trend.ema_indicator(data['close'], window=params['average_period'])  
elif 'WMA' == params['average_type']:
    data['average'] = ta.trend.wma_indicator(data['close'], window=params['average_period'])   
else:
    raise ValueError(f"The average type {params['average_type']} is not supported")

for i, e in enumerate(params['envelopes']):
    data[f'band_low_{i + 1}'] = data['average'] * (1 - e)
print(f"{datetime.now().strftime('%H:%M:%S')}: ohlcv data fetched")

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

# --- CHECK IF A POSITION IS OPEN ---
open_orders = bitget.fetch_open_orders(params['symbol'])
open_position = True if len(open_orders) > 0 else False
if open_position:
    position = open_orders[0]  # Assuming there's only one open order
    print(f"{datetime.now().strftime('%H:%M:%S')}: {position['side']} position is open")

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

# --- PLACE ORDERS DEPENDING ON HOW MANY BANDS HAVE ALREADY BEEN HIT ---
if open_position:
    long_ok = True if 'buy' == position['side'] else False
    range_longs = range(len(params['envelopes']) - len([o for o in open_orders if o['side'] == 'buy']), len(params['envelopes']))
else:
    long_ok = True
    range_longs = range(len(params['envelopes']))

if long_ok:
    for i in range_longs:
        amount = balance / len(params['envelopes']) / data[f'band_low_{i + 1}'].iloc[-1]
        min_amount = bitget.fetch_min_amount_tradable(params['symbol'])
        if amount >= min_amount:
            # entry (buy limit order at lower envelope band)
            bitget.place_trigger_limit_order(
                symbol=params['symbol'],
                side='buy',
                amount=amount,
                trigger_price=(1 + trigger_price_delta) * data[f'band_low_{i + 1}'].iloc[-1],
                price=data[f'band_low_{i + 1}'].iloc[-1],
                print_error=True,
            )
            print(f"{datetime.now().strftime('%H:%M:%S')}: placed open long trigger limit order of {amount}, trigger price {1.005 * data[f'band_low_{i + 1}'].iloc[-1]}, price {data[f'band_low_{i + 1}'].iloc[-1]}")
else:
    print(f"{datetime.now().strftime('%H:%M:%S')}: /!\\ long orders not placed for envelope {i+1}, amount {amount} smaller than minimum requirement {min_amount}")

# If a position is open, place TP and SL orders
if open_position:
    if position['side'] == 'buy':
        close_side = 'sell'
        stop_loss_price = float(position['price']) * (1 - params['stop_loss_pct'])
        take_profit_price = data['average'].iloc[-1]

        amount = position['amount']
        # exit (take profit - sell limit order at the average price)
        bitget.place_limit_order(
            symbol=params['symbol'],
            side=close_side,
            amount=amount,
            price=take_profit_price,
            print_error=True,
        )
        print(f"{datetime.now().strftime('%H:%M:%S')}: placed exit long limit order of {amount}, price {take_profit_price}")
        
        # stop loss (stop-limit order)
        sl_order = bitget.place_stop_limit_order(
            symbol=params['symbol'],
            side=close_side,
            amount=amount,
            stop_price=stop_loss_price,
            limit_price=stop_loss_price * (1 - 0.005),  # Small margin below stop price to ensure execution
            print_error=True,
        )
        info = {
            "status": "ok_to_trade",
            "last_side": position['side'],
            "stop_loss_price": stop_loss_price,
            "stop_loss_ids": [sl_order['id']],
        }
        print(f"{datetime.now().strftime('%H:%M:%S')}: placed stop loss stop-limit order of {amount}, stop price {stop_loss_price}, limit price {stop_loss_price * (1 - 0.005)}")
else:
    info = {
        "status": "ok_to_trade",
        "last_side": tracker_info['last_side'],
        "stop_loss_ids": [],
    }

update_tracker_file(tracker_file, info)
print(f"{datetime.now().strftime('%H:%M:%S')}: <<< all done")
