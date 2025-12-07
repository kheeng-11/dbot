"""
SMC Deriv Bot v2 Optimized:
 - POI retest confirmation
 - Ignores mitigated zones
 - One trade per direction
 - Martingale system
 - Profit target + cooldown
 - MA6 filter
 - Optimized calculations for ATR and MA6
"""

import websocket
import json
import time
import threading
import logging
import numpy as np
from collections import deque

# ============= CONFIG =============
APP_ID = "64396"
API_TOKEN = "yrCmyrMCQgXfZWi"   # keep private
SYMBOL = "R_75"
BASE_STAKE = 100
STAKE = BASE_STAKE
MARTINGALE_MULTIPLIER = 2.0
DURATION = 5               # ticks
BARRIER_BUY = +0.7777
BARRIER_SELL = +0.7777
PROFIT_TARGET = 10000.0
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"

# SMC params
SWING_LENGTH = 10
LTF_WINDOW = 120
HISTORY_TO_KEEP = 40
BOX_WIDTH = 2.5
ATR_WINDOW = 14
POI_TOLERANCE_FACTOR = 0.20
RETEST_LOOKAHEAD = 300
CONFIRMATION_LOOKAHEAD = 3
COOLDOWN_BETWEEN_TRADES = 0.6
MAX_PRICE_HISTORY = 4000

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("smc_martingale_bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("SMC_Martingale_Bot")

# Global state
running = True
price_history = deque(maxlen=MAX_PRICE_HISTORY)
last_trade_direction = None
last_trade_time = 0.0
total_profit = 0.0
cooldown_until = 0.0

# ==================== SMC Helpers ====================
def compute_atr(prices, window=ATR_WINDOW):
    if len(prices) < 2:
        return 0.0
    diffs = np.abs(np.diff(prices))
    if len(diffs) < window:
        return float(np.mean(diffs))
    return float(np.mean(diffs[-window:]))

def compute_ma(prices, period=6):
    if len(prices) < period:
        return None
    return float(np.mean(prices[-period:]))

def find_swing_points(prices, swing_length=SWING_LENGTH):
    N = len(prices)
    L = swing_length
    if N < L * 2 + 1:
        return [], []
    highs, lows = [], []
    for i in range(L, N - L):
        segment = prices[i - L:i + L + 1]
        if prices[i] == max(segment):
            highs.append({'index': i, 'value': prices[i]})
        if prices[i] == min(segment):
            lows.append({'index': i, 'value': prices[i]})
    return highs, lows

def create_zones(prices, highs, lows, atr_val, history_to_keep=HISTORY_TO_KEEP):
    supply_zones, demand_zones = [], []
    atr = atr_val if atr_val > 0 else max(prices) - min(prices) * 0.01
    for sh in highs[-history_to_keep:]:
        buffer = atr * (BOX_WIDTH / 10.0)
        top = sh['value']
        bottom = top - buffer
        poi = (top + bottom) / 2.0
        supply_zones.append({'index': sh['index'], 'top': top, 'bottom': bottom, 'poi': poi})
    for sl in lows[-history_to_keep:]:
        buffer = atr * (BOX_WIDTH / 10.0)
        bottom = sl['value']
        top = bottom + buffer
        poi = (top + bottom) / 2.0
        demand_zones.append({'index': sl['index'], 'top': top, 'bottom': bottom, 'poi': poi})
    return supply_zones, demand_zones

def detect_bos(latest_index, price, supply_zones, demand_zones):
    bos_events = []
    for z in supply_zones:
        if price > z['top']:
            bos_events.append({'type': 'BOS High', 'zone': z, 'price_index': latest_index, 'price': price})
    for z in demand_zones:
        if price < z['bottom']:
            bos_events.append({'type': 'BOS Low', 'zone': z, 'price_index': latest_index, 'price': price})
    return bos_events

# ==================== BOS Watch ====================
class BOSWatch:
    def __init__(self, bos_type, zone, bos_index, deadline_index):
        self.type = bos_type
        self.zone = zone
        self.bos_index = bos_index
        self.retest_index = None
        self.confirmation_index = None
        self.deadline_index = deadline_index
        self.mitigated = False

    def check_mitigation(self, price):
        if self.type == 'BOS High' and price < self.zone['bottom']:
            self.mitigated = True
        elif self.type == 'BOS Low' and price > self.zone['top']:
            self.mitigated = True
        return self.mitigated

    def check_retest_and_confirm(self, prices, current_index):
        if self.mitigated:
            return False
        poi = self.zone['poi']
        tol = max(self.zone['top'] - self.zone['bottom'], 1e-9) * POI_TOLERANCE_FACTOR

        if self.retest_index is None:
            for i in range(self.bos_index + 1, current_index + 1):
                if poi - tol <= prices[i] <= poi + tol:
                    self.retest_index = i
                    break

        if self.retest_index is not None and self.confirmation_index is None:
            for j in range(self.retest_index, min(len(prices), self.retest_index + CONFIRMATION_LOOKAHEAD)):
                p = prices[j]
                if self.type == 'BOS High' and p > poi + (tol * 0.15):
                    self.confirmation_index = j
                    return True
                elif self.type == 'BOS Low' and p < poi - (tol * 0.15):
                    self.confirmation_index = j
                    return True
        return False

# ==================== Deriv Helpers ====================
def place_trade(ws, contract_type, barrier, stake):
    try:
        proposal = {
            "proposal": 1, "amount": stake, "basis": "stake", "contract_type": contract_type,
            "currency": "USD", "duration": DURATION, "duration_unit": "t", "symbol": SYMBOL,
            "barrier": f"{barrier:+.3f}"
        }
        ws.send(json.dumps(proposal))
        prop = json.loads(ws.recv())
        if "error" in prop:
            log.error("Proposal error: %s", prop["error"]["message"])
            return None
        pid = prop["proposal"]["id"]
        ws.send(json.dumps({"buy": pid, "price": stake}))
        buy_res = json.loads(ws.recv())
        if "error" in buy_res:
            log.error("Buy error: %s", buy_res["error"]["message"])
            return None
        cid = buy_res["buy"]["contract_id"]
        log.info("✅ Trade placed: %s | stake=%.2f | id=%s", contract_type, stake, cid)
        return cid
    except Exception as e:
        log.exception("place_trade exception: %s", e)
        return None

def wait_for_result(ws, contract_id):
    try:
        ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1}))
        while True:
            msg = json.loads(ws.recv())
            if "proposal_open_contract" in msg:
                poc = msg["proposal_open_contract"]
                if poc.get("is_sold"):
                    pnl = float(poc["profit"])
                    result = "✅ WIN" if pnl > 0 else "❌ LOSS"
                    log.info("%s | Profit: %.2f", result, pnl)
                    return pnl
    except Exception as e:
        log.exception("wait_for_result exception: %s", e)
        return None

# ==================== Main Trading Loop ====================
def trading_loop(ws):
    global last_trade_direction, last_trade_time, STAKE, total_profit, cooldown_until

    absolute_index = 0
    watching_bos = []

    while running:
        try:
            msg = json.loads(ws.recv())
            if "tick" in msg:
                price = float(msg["tick"]["quote"])
                price_history.append(price)
                absolute_index += 1

                if len(price_history) < max(LTF_WINDOW, 6):
                    continue

                prices_arr = list(price_history)

                # Compute indicators only once per tick
                atr_val = compute_atr(prices_arr)
                ma6 = compute_ma(prices_arr, 6)

                swings_h, swings_l = find_swing_points(prices_arr[-LTF_WINDOW:])
                supply_zones, demand_zones = create_zones(prices_arr, swings_h, swings_l, atr_val)
                latest_idx = len(prices_arr) - 1
                bos_events = detect_bos(latest_idx, price, supply_zones, demand_zones)

                # Register new BOS
                for e in bos_events:
                    if not any(w.zone['poi'] == e['zone']['poi'] and w.type == e['type'] for w in watching_bos):
                        deadline = latest_idx + RETEST_LOOKAHEAD
                        watching_bos.append(BOSWatch(e['type'], e['zone'], e['price_index'], deadline))
                        log.info("Registered BOS %s at price=%.5f, poi=%.5f", e['type'], e['price'], e['zone']['poi'])

                # Process active BOS watches
                active = []
                for w in watching_bos:
                    w.check_mitigation(price)
                    if w.check_retest_and_confirm(prices_arr, latest_idx):
                        direction = 'BUY' if w.type == 'BOS High' else 'SELL'
                        contract_type = "CALL" if direction == 'BUY' else "PUT"
                        barrier = BARRIER_BUY if direction == 'BUY' else BARRIER_SELL
                        now = time.time()

                        # --- MA6 filter ---
                        if ma6 is not None:
                            if direction == 'BUY' and price < ma6:
                                log.info("Skipping BUY -> price %.5f below MA6 %.5f", price, ma6)
                                continue
                            elif direction == 'SELL' and price > ma6:
                                log.info("Skipping SELL -> price %.5f above MA6 %.5f", price, ma6)
                                continue

                        # Skip if profit cooldown is active
                        if now < cooldown_until:
                            continue

                        # One trade per direction
                        if direction != last_trade_direction and (now - last_trade_time >= COOLDOWN_BETWEEN_TRADES):
                            log.info("CONFIRMED %s retest -> placing %s with stake %.2f", w.type, contract_type, STAKE)
                            cid = place_trade(ws, contract_type, barrier, STAKE)
                            if cid:
                                pnl = wait_for_result(ws, cid)
                                last_trade_direction = direction
                                last_trade_time = time.time()

                                # Martingale handling
                                if pnl is not None:
                                    total_profit += pnl
                                    if pnl < 0:
                                        STAKE *= MARTINGALE_MULTIPLIER
                                        log.warning("Loss -> Martingale next stake: %.2f", STAKE)
                                    else:
                                        STAKE = BASE_STAKE
                                        log.info("Win -> Stake reset to base %.2f", BASE_STAKE)

                                    # Profit target cooldown
                                    if total_profit >= PROFIT_TARGET:
                                        cooldown_until = time.time() + 5 * 3600
                                        log.info("🎯 Profit target %.2f reached! Cooling for 5 hours.", total_profit)
                                        total_profit = 0.0
                            else:
                                log.warning("Trade placement failed.")
                        continue

                    if not w.mitigated:
                        active.append(w)
                watching_bos = active

        except websocket.WebSocketConnectionClosedException:
            log.warning("WebSocket closed — reconnecting soon.")
            break
        except KeyboardInterrupt:
            log.info("Manual stop.")
            break
        except Exception as e:
            log.exception("Runtime error: %s", e)
            time.sleep(1)

# ==================== Connection & Keepalive ====================
def keepalive(ws):
    while running:
        try:
            ws.send(json.dumps({"ping": 1}))
        except Exception:
            break
        time.sleep(20)

def authorize(ws):
    ws.send(json.dumps({"authorize": API_TOKEN}))
    res = json.loads(ws.recv())
    if "error" in res:
        log.error("Authorization failed: %s", res["error"].get("message"))
        return False
    log.info("Authorized as %s", res["authorize"].get("loginid"))
    return True

def subscribe_ticks(ws):
    ws.send(json.dumps({"ticks": SYMBOL}))
    log.info("Subscribed to ticks: %s", SYMBOL)

def connect_and_run():
    global running
    while running:
        try:
            log.info("Connecting to Deriv...")
            ws = websocket.create_connection(WS_URL, timeout=10)
            if not authorize(ws):
                ws.close()
                time.sleep(5)
                continue
            threading.Thread(target=keepalive, args=(ws,), daemon=True).start()
            subscribe_ticks(ws)
            trading_loop(ws)
        except Exception as e:
            log.exception("Connection error: %s", e)
            time.sleep(5)
        finally:
            try:
                ws.close()
            except Exception:
                pass
            log.info("Reconnecting in 3 seconds...")
            time.sleep(3)

if __name__ == "__main__":
    log.info("🚀 Starting Optimized SMC Deriv Bot v2 with MA6, Martingale, Profit Target, Cooldown, Mitigated Zones")
    connect_and_run()
