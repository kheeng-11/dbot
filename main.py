import asyncio
import websockets
import json
from collections import deque
import statistics

API_TOKEN = "aiKRjkAWvtFVO6m"
DERIV_API = "wss://ws.derivws.com/websockets/v3?app_id=64396"

SYMBOL = "R_75"
BARRIER_OFFSET = "+200.5555"
STAKE_AMOUNT = 10
DURATION = 1
DURATION_UNIT = "m"
CURRENCY = "USD"

MA_FAST_PERIOD = 10
MA_20_PERIOD = 20
MA_50_PERIOD = 50
BB_PERIOD = 20
ATR_PERIOD = 14

VOLATILITY_THRESHOLD = 15.5
MA_GAP_THRESHOLD = 4.0
BB_WIDTH_THRESHOLD = 90  # Smaller means more consolidation, adjust as needed

price_history_fast = deque(maxlen=MA_FAST_PERIOD)
price_history_20 = deque(maxlen=MA_20_PERIOD)
price_history_50 = deque(maxlen=MA_50_PERIOD)
bb_prices = deque(maxlen=BB_PERIOD)
atr_prices = deque(maxlen=ATR_PERIOD + 1)  # ATR needs previous close

contract_running = False
last_spot = None
volatility_session_active = False

def calculate_bollinger_bands(prices, period=20, num_std_dev=2):
    sma = sum(prices) / period
    std_dev = statistics.stdev(prices)
    upper = sma + (num_std_dev * std_dev)
    lower = sma - (num_std_dev * std_dev)
    return upper, lower, upper - lower  # Return also BB width

def calculate_atr(prices):
    trs = []
    for i in range(1, len(prices)):
        tr = abs(prices[i] - prices[i-1])
        trs.append(tr)
    return sum(trs) / len(trs)

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

                if (len(price_history_fast) < MA_FAST_PERIOD or
                    len(price_history_20) < MA_20_PERIOD or
                    len(price_history_50) < MA_50_PERIOD or
                    len(bb_prices) < BB_PERIOD or
                    len(atr_prices) < ATR_PERIOD + 1):
                    last_spot = spot_price
                    continue

                ma_fast = sum(price_history_fast) / MA_FAST_PERIOD
                ma20 = sum(price_history_20) / MA_20_PERIOD
                ma50 = sum(price_history_50) / MA_50_PERIOD
                ma_gap = abs(ma20 - ma50)

                bb_upper, bb_lower, bb_width = calculate_bollinger_bands(bb_prices, BB_PERIOD)
                atr = calculate_atr(atr_prices)

                print(f"Spot: {spot_price} | MA Gap: {round(ma_gap, 5)} | BB Width: {round(bb_width, 5)} | ATR: {round(atr, 5)}")

                if ma_gap < MA_GAP_THRESHOLD:
                    print("⚠️ Skipping: MA gap too small.")
                    last_spot = spot_price
                    continue

                if not (spot_price < ma20 and spot_price < ma50 and ma20 < ma50):
                    last_spot = spot_price
                    continue

                # ✅ Prefer trades when BB width is *small* (consolidation) and ATR is *low* (low volatility)
                if bb_width > BB_WIDTH_THRESHOLD:
                    print("❌ Skipping: Bollinger Bands too wide → Not in consolidation.")
                    last_spot = spot_price
                    continue

                if atr > VOLATILITY_THRESHOLD:
                    print("❌ Skipping: ATR too high → Volatility unacceptable.")
                    last_spot = spot_price
                    continue

                if not contract_running and not volatility_session_active:
                    volatility_session_active = True
                    print("✅ Consolidation detected → Starting volatility session...")

                if volatility_session_active:
                    if last_spot is not None:
                        diff = abs(spot_price - last_spot)
                        if diff > VOLATILITY_THRESHOLD:
                            print(f"🚫 Volatility spike detected → Discarding → Change: {diff}")
                            volatility_session_active = False
                            last_spot = spot_price
                            continue

                    print(f"📉 All confirmations OK → Proposing PUT... {diff}")
                    await propose_and_buy(websocket, "PUT")
                    volatility_session_active = False

                last_spot = spot_price

            elif data.get("msg_type") == "error":
                print("❌ API Error:", data['error']['message'])
                break

async def propose_and_buy(websocket, contract_type):
    global contract_running

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
            print("⏳ Waiting 10s before restarting session...")
            await asyncio.sleep(10)
            contract_running = False
        else:
            print("⚠️ Buy failed:", buy_data)

asyncio.run(deriv_bot())
