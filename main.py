import asyncio
import websockets
import json
import statistics
from collections import deque
import time
import requests

# === CONFIG ===
API_TOKEN = "aiKRjkAWvtFVO6m"
DERIV_API = "wss://ws.derivws.com/websockets/v3?app_id=64396"

SYMBOL = "R_75"
INITIAL_STAKE = 10
DURATION = 1
DURATION_UNIT = "m"
CURRENCY = "USD"
COOLDOWN = 5
MIN_CONFIDENCE = 70

BARRIER_CALL = "-200.9999"
BARRIER_PUT = "+200.9999"

# === STATE VARIABLES ===
price_history = deque(maxlen=60)
last_prices = deque(maxlen=3)
last_trade_time = 0
current_stake = INITIAL_STAKE
is_trading = False

# === TELEGRAM ===
TELEGRAM_TOKEN = "8133122189:AAFygYKQ1c2wW1bWaG1HtbhuwzjnzzZf5Ag"
TELEGRAM_CHAT_ID = "6054213404"

# === UTILITY FUNCTIONS ===
def moving_average(prices, period):
    if len(prices) < period:
        return None
    return sum(list(prices)[-period:]) / period

def calculate_bollinger_width(prices, period=20):
    if len(prices) < period:
        return 0
    sma = moving_average(prices, period)
    std_dev = statistics.stdev(list(prices)[-period:])
    return (sma + 2 * std_dev) - (sma - 2 * std_dev)

def detect_trend(prices):
    ma10 = moving_average(prices, 10)
    ma20 = moving_average(prices, 20)
    ma50 = moving_average(prices, 50)
    if None in (ma10, ma20, ma50):
        return None
    if ma10 > ma20 > ma50:
        return "CALL"
    elif ma10 < ma20 < ma50:
        return "PUT"
    return None

def is_moving_same_direction():
    if len(last_prices) < 3:
        return False
    diffs = [last_prices[i] - last_prices[i - 1] for i in range(1, len(last_prices))]
    return all(d > 0 for d in diffs) or all(d < 0 for d in diffs)

def calculate_confidence(prices, last_prices):
    score = 0
    ma10 = moving_average(prices, 10)
    ma20 = moving_average(prices, 20)
    ma50 = moving_average(prices, 50)

    if ma10 and ma20 and ma50:
        if ma10 > ma20 > ma50 or ma10 < ma20 < ma50:
            gap1 = abs(ma10 - ma20)
            gap2 = abs(ma20 - ma50)
            if gap1 > 0.1 and gap2 > 0.1:
                score += 40
            elif gap1 > 0.05 or gap2 > 0.05:
                score += 20

    width = calculate_bollinger_width(prices)
    if width < 0.2:
        score += 30  # Prefer tight bands (low volatility)
    elif width < 0.3:
        score += 15
    else:
        score -= 20  # Penalize high volatility

    if is_moving_same_direction():
        score += 20

    if len(last_prices) >= 2:
        delta = abs(last_prices[-1] - last_prices[-2])
        if delta > 0.2:
            score += 20
        elif delta > 0.1:
            score += 10

    return score

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram Error:", e)

# === TRADE EXECUTION ===
async def send_trade(ws, queue, contract_type, barrier):
    global current_stake, is_trading
    try:
        print(f"🎯 Entering {contract_type} trade at ${current_stake}")
        send_telegram(f"🚀 Placing {contract_type} trade at ${current_stake} on {SYMBOL}")

        await ws.send(json.dumps({
            "proposal": 1,
            "amount": current_stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": CURRENCY,
            "duration": DURATION,
            "duration_unit": DURATION_UNIT,
            "symbol": SYMBOL,
            "barrier": barrier
        }))

        proposal = await wait_for(queue, "proposal")
        pid = proposal['proposal']['id']

        await ws.send(json.dumps({
            "buy": pid,
            "price": current_stake
        }))
        buy_data = await wait_for(queue, "buy")
        cid = buy_data["buy"]["contract_id"]

        await ws.send(json.dumps({
            "proposal_open_contract": 1,
            "contract_id": cid,
            "subscribe": 1
        }))

        while True:
            poc = await wait_for(queue, "proposal_open_contract")
            contract = poc["proposal_open_contract"]
            sub_id = poc.get("subscription", {}).get("id")
            if contract.get("is_sold"):
                profit = contract["profit"]
                status = contract["status"]
                result_msg = f"🏁 Trade closed | Profit: ${profit:.2f} | Result: {status.upper()}"
                print(result_msg)
                send_telegram(result_msg)

                if sub_id:
                    await ws.send(json.dumps({"forget": sub_id}))
                return status
            await asyncio.sleep(1)
    finally:
        is_trading = False

async def wait_for(queue, msg_type):
    while True:
        data = await queue.get()
        if data.get("msg_type") == msg_type:
            return data

# === MAIN SNIPER LOOP ===
async def run_sniper():
    global last_trade_time, current_stake, is_trading
    async with websockets.connect(DERIV_API) as ws:
        queue = asyncio.Queue()

        async def receiver():
            async for msg in ws:
                await queue.put(json.loads(msg))

        asyncio.create_task(receiver())

        await ws.send(json.dumps({"authorize": API_TOKEN}))
        await wait_for(queue, "authorize")

        await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))

        while True:
            tick = await wait_for(queue, "tick")
            spot = tick['tick']['quote']
            price_history.append(spot)
            last_prices.append(spot)
            print(f"\nTick: {spot}")

            if is_trading:
                print("⏳ Currently trading... waiting to finish")
                continue

            if time.time() - last_trade_time < COOLDOWN:
                print("⏳ Cooldown active")
                continue

            if len(last_prices) < 3:
                print("⌛ Waiting for direction data...")
                continue

            if not is_moving_same_direction():
                print(f"⛔ Direction unclear → skip | Last: {[round(p, 2) for p in last_prices]}")
                continue

            trend = detect_trend(price_history)
            if not trend:
                print("⛔ No trend detected → skip")
                continue

            volatility = calculate_bollinger_width(price_history)
            if volatility > 0.4:
                print(f"🚫 High volatility ({volatility:.2f}) — skipping")
                continue

            confidence = calculate_confidence(price_history, last_prices)
            print(f"🧠 Confidence Score: {confidence}%")
            if confidence < MIN_CONFIDENCE:
                print("⚠️ Confidence too low → skip")
                continue

            barrier = BARRIER_CALL if trend == "CALL" else BARRIER_PUT
            is_trading = True
            result = await send_trade(ws, queue, trend, barrier)

            if result == "won":
                current_stake = INITIAL_STAKE
            else:
                current_stake *= 1  # No martingale change for now

            last_trade_time = time.time()
            last_prices.clear()
            await asyncio.sleep(6)

# === RUN ===
asyncio.run(run_sniper())
