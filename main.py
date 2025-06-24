import asyncio
import websockets
import json
from collections import deque
import statistics

API_TOKEN = "aiKRjkAWvtFVO6m"
DERIV_API = "wss://ws.derivws.com/websockets/v3?app_id=64396"

SYMBOL = "R_75"
BARRIER_OFFSET = "+0.0001"
STAKE_BASE = 0.35
STAKE_AMOUNT = STAKE_BASE
MARTINGALE_MULTIPLIER = 2
MAX_LOSS_COUNT = 5
DURATION = 5
DURATION_UNIT = "t"
CURRENCY = "USD"

MA_FAST_PERIOD = 10
MA_20_PERIOD = 20
MA_50_PERIOD = 50
BB_PERIOD = 20
ATR_PERIOD = 14
MACD_SHORT = 12
MACD_LONG = 26

VOLATILITY_THRESHOLD = 15.5
MA_GAP_THRESHOLD = 4.0
BB_WIDTH_THRESHOLD = 90

price_history_fast = deque(maxlen=MA_FAST_PERIOD)
price_history_20 = deque(maxlen=MA_20_PERIOD)
price_history_50 = deque(maxlen=MA_50_PERIOD)
bb_prices = deque(maxlen=BB_PERIOD)
atr_prices = deque(maxlen=ATR_PERIOD + 1)
macd_prices = deque(maxlen=MACD_LONG)
macd_history = deque(maxlen=2)

contract_running = False
last_spot = None
volatility_session_active = False
loss_count = 0

def calculate_bollinger_bands(prices, period=20, num_std_dev=2):
    sma = sum(prices) / period
    std_dev = statistics.stdev(prices)
    upper = sma + (num_std_dev * std_dev)
    lower = sma - (num_std_dev * std_dev)
    return upper, lower, upper - lower

def calculate_atr(prices):
    trs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    return sum(trs) / len(trs)

def calculate_ema(prices, period):
    ema = prices[0]
    k = 2 / (period + 1)
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_macd(prices):
    if len(prices) < MACD_LONG:
        return 0
    ema_short = calculate_ema(list(prices)[-MACD_SHORT:], MACD_SHORT)
    ema_long = calculate_ema(list(prices)[-MACD_LONG:], MACD_LONG)
    return ema_short - ema_long

def is_falling(series):
    return series[0] >= series[1]

async def deriv_bot():
    global contract_running, last_spot, volatility_session_active

    async with websockets.connect(DERIV_API) as websocket:
        await websocket.send(json.dumps({"authorize": API_TOKEN}))
        print(await websocket.recv())

        await websocket.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))

        while True:
            response = await websocket.recv()
            data = json.loads(response)

            if data.get("msg_type") == "tick":
                spot_price = float(data['tick']['quote'])
                price_history_fast.append(spot_price)
                price_history_20.append(spot_price)
                price_history_50.append(spot_price)
                bb_prices.append(spot_price)
                atr_prices.append(spot_price)
                macd_prices.append(spot_price)

                if (len(price_history_fast) < MA_FAST_PERIOD or
                    len(price_history_20) < MA_20_PERIOD or
                    len(price_history_50) < MA_50_PERIOD or
                    len(bb_prices) < BB_PERIOD or
                    len(atr_prices) < ATR_PERIOD + 1 or
                    len(macd_prices) < MACD_LONG):
                    last_spot = spot_price
                    continue

                ma_fast = sum(price_history_fast) / MA_FAST_PERIOD
                ma20 = sum(price_history_20) / MA_20_PERIOD
                ma50 = sum(price_history_50) / MA_50_PERIOD
                ma_gap = abs(ma20 - ma50)

                _, _, bb_width = calculate_bollinger_bands(bb_prices, BB_PERIOD)
                atr = calculate_atr(atr_prices)
                macd_value = calculate_macd(macd_prices)
                macd_history.append(macd_value)

                print(f"Spot: {spot_price} | MA Gap: {round(ma_gap, 5)} | BB Width: {round(bb_width, 5)} | ATR: {round(atr, 5)} | MACD: {round(macd_value, 5)}")

                if (ma_gap < MA_GAP_THRESHOLD or
                    not (spot_price < ma20 and spot_price < ma50 and ma20 < ma50) or
                    bb_width > BB_WIDTH_THRESHOLD or
                    atr > VOLATILITY_THRESHOLD or
                    macd_value >= 0 or
                    len(macd_history) < 2 or
                    not is_falling(list(macd_history)) or
                    loss_count >= MAX_LOSS_COUNT):
                    last_spot = spot_price
                    continue

                if not contract_running and not volatility_session_active:
                    volatility_session_active = True
                    print("✅ All conditions met → Starting volatility session...")

                if volatility_session_active:
                    if last_spot is not None:
                        diff = abs(spot_price - last_spot)
                        if diff > VOLATILITY_THRESHOLD:
                            print(f"🚫 Volatility spike detected → Discarding → Change: {diff}")
                            volatility_session_active = False
                            last_spot = spot_price
                            continue

                    print("📉 Confirmed → Proposing PUT...")
                    await propose_and_buy(websocket, "PUT")
                    volatility_session_active = False

                last_spot = spot_price

            elif data.get("msg_type") == "error":
                print("❌ API Error:", data['error']['message'])
                break

async def propose_and_buy(websocket, contract_type):
    global contract_running, STAKE_AMOUNT, loss_count

    await websocket.send(json.dumps({
        "proposal": 1,
        "amount": STAKE_AMOUNT,
        "basis": "stake",
        "contract_type": contract_type,
        "currency": CURRENCY,
        "duration": DURATION,
        "duration_unit": DURATION_UNIT,
        "symbol": SYMBOL,
        "barrier": BARRIER_OFFSET
    }))

    proposal_response = await websocket.recv()
    data = json.loads(proposal_response)

    if data.get("proposal"):
        proposal_id = data['proposal']['id']
        print(f"Proposal OK → Buying: {proposal_id}")

        await websocket.send(json.dumps({
            "buy": proposal_id,
            "price": STAKE_AMOUNT
        }))

        buy_response = await websocket.recv()
        buy_data = json.loads(buy_response)

        if buy_data.get("buy"):
            print("✅ TRADE PLACED:", buy_data["buy"])
            contract_running = True
            contract_id = buy_data["buy"]["contract_id"]
            await monitor_contract(websocket, contract_id)

            print("⏳ Waiting 10s before restarting session...")
            await asyncio.sleep(10)
            contract_running = False
        else:
            print("⚠️ Buy failed:", buy_data)

async def monitor_contract(websocket, contract_id):
    global STAKE_AMOUNT, loss_count

    await websocket.send(json.dumps({
        "proposal_open_contract": 1,
        "contract_id": contract_id,
        "subscribe": 1
    }))

    while True:
        response = await websocket.recv()
        data = json.loads(response)

        if data.get("msg_type") == "proposal_open_contract":
            if data["proposal_open_contract"]["is_sold"]:
                profit = data["proposal_open_contract"]["profit"]
                if profit > 0:
                    print(f"✅ CONTRACT WON → Profit: {profit}")
                    STAKE_AMOUNT = STAKE_BASE
                    loss_count = 0
                else:
                    print(f"❌ CONTRACT LOST → Loss: {profit}")
                    loss_count += 1
                    STAKE_AMOUNT = STAKE_BASE * (MARTINGALE_MULTIPLIER ** loss_count)
                    print(f"➡️ Next stake: {STAKE_AMOUNT}")
                break

asyncio.run(deriv_bot())
