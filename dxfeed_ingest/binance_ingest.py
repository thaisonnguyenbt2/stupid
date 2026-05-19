import os
import time
import json
import threading
import websocket
import logging
import ssl
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv, find_dotenv

from indicators import OrderFlowTracker
from telegram_notifier import send_telegram_message
from capital_executor import CapitalClient, CAPITAL_LIVE_ENABLED
from pymongo import MongoClient

# Load configuration
load_dotenv(find_dotenv())
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/trading')

SYMBOL = 'PAXGUSDT'

# R:R Slots
RR_SLOTS = [
    {'name': 'A', 'tp_mult': 1.0, 'sl_mult': 1.0, 'label': '1:1'},
    {'name': 'B', 'tp_mult': 3.0, 'sl_mult': 1.0, 'label': '3:1'},
    {'name': 'C', 'tp_mult': 2.0, 'sl_mult': 1.0, 'label': '2:1'},
]

# Setup Logging
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_file = os.path.join(os.path.dirname(__file__), 'binance_ingest.log')
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Initialize Capital.com Execution Client
capital_client = CapitalClient() if CAPITAL_LIVE_ENABLED else None

class TradingStateMachine:
    def __init__(self):
        self.state = "SCANNING"  # SCANNING, PENDING_LONG, PENDING_SHORT
        self.pending_setup_candle = None
        self.last_trade_time = 0  # For 3-minute execution guard

    def execute_grid(self, db, direction: str, entry_price: float, sl_price: float, atr: float, vwap: float):
        """Execute the 3 RR_SLOTS grid and save to MongoDB."""
        now = time.time()
        
        # 3-Minute Execution Guard
        if now - self.last_trade_time < 180:
            logger.info("⛔ Blocked by 3-Minute Execution Guard. Skipping grid.")
            return
            
        self.last_trade_time = now
        
        # Determine exact SL distance in dollars for RR calculation
        sl_dist = abs(entry_price - sl_price)
        # Fallback if SL distance is suspiciously small
        if sl_dist < (atr * 0.5):
            sl_dist = atr * 0.5
            sl_price = entry_price - sl_dist if direction == 'LONG' else entry_price + sl_dist
            
        for slot in RR_SLOTS:
            tp_dist = sl_dist * slot['tp_mult']
            
            if direction == 'LONG':
                exec_tp = entry_price + tp_dist
            else:
                exec_tp = entry_price - tp_dist
                
            doc = {
                'symbol': SYMBOL,
                'direction': direction,
                'status': 'OPEN',
                'entryPrice': round(entry_price, 2),
                'tp': round(exec_tp, 2),
                'sl': round(sl_price, 2),
                'entryTime': int(now * 1000),
                'signalType': 'ORDER_FLOW_ABSORPTION',
                'lotSize': 0.01,
                'contextTf': f"{slot['name']}({slot['label']})",
                'isArchived': False,
            }
            
            # Route Execution & Notifications
            msg = f"🚨 <b>NEW SIGNAL ({slot['name']})</b>\n{direction} @ {entry_price:.2f}\nTP: {exec_tp:.2f} | SL: {sl_price:.2f}"
            
            if slot['name'] == 'C':  # 2:1 R:R Main Slot
                send_telegram_message(msg, target_chat='default')
                
                # Send API Execution
                if capital_client:
                    cap_result = capital_client.open_trade(
                        direction=direction, lot_size=0.01, tp=exec_tp, sl=sl_price,
                        strategy='ORDER_FLOW_ABSORPTION', slot=slot['name']
                    )
                    if cap_result.get('error'):
                        doc['status'] = 'FAILED'
                        logger.error(f"⛔ Capital.com rejected slot C: {cap_result['error']}")
                    else:
                        doc['capitalDealRef'] = cap_result.get('dealReference', '')
                        doc['capitalStatus'] = cap_result.get('status', 'SENT')
                        logger.info(f"✅ Capital.com executed slot C successfully.")
                else:
                     logger.info(f"PAPER TRADE executed slot C.")
                     
            elif slot['name'] == 'B':  # 3:1 R:R
                send_telegram_message(msg, target_chat='2')
                logger.info(f"📲 Telegram sent for slot B (3:1). No Capital execution.")
                doc['capitalStatus'] = 'TELEGRAM_ONLY'
                
            elif slot['name'] == 'A':  # 1:1 R:R
                send_telegram_message(msg, target_chat='3')
                logger.info(f"📲 Telegram sent for slot A (1:1). No Capital execution.")
                doc['capitalStatus'] = 'TELEGRAM_ONLY'
            
            if db is not None:
                db.paper_trades.insert_one(doc)

    def evaluate(self, tracker: OrderFlowTracker, db, snap):
        # 1. Get Session Context
        vwap = tracker.session.get_vwap()
        va = tracker.session.get_value_area()
        vpoc, vah, val = va['vpoc'], va['vah'], va['val']
        
        # 2. Get Rolling Metrics
        avg_vol = tracker.get_avg_volume()
        avg_delta = tracker.get_avg_delta()
        atr = tracker.get_atr()
        
        close = snap['close']
        high = snap['high']
        low = snap['low']
        delta = snap['delta']

        if avg_vol == 0 or atr == 0 or close == 0.0:
            return # Need more history or trading volume
            
        # State Machine Logic
        if self.state == "SCANNING":
            # --- LONG SETUP RULES ---
            # Context: price > VWAP, price near VAL
            is_above_vwap = close > vwap
            is_near_val = abs(close - val) < (atr * 0.5)
            
            # Pressure: delta very negative
            is_delta_very_negative = delta < -abs(avg_delta * 1.5)
            if avg_delta == 0 and delta < -5.0: # Fallback
                is_delta_very_negative = True
                
            # Response: price not falling (close near high means absorption buy)
            is_close_near_high = (high - close) <= (atr * 0.2)
            
            if is_above_vwap and is_near_val and is_delta_very_negative and is_close_near_high:
                msg = f"⏳ <b>PENDING LONG</b>\nPrice: {close:.2f}\nVWAP: {vwap:.2f}\nVAL: {val:.2f}\nDelta: {delta:.2f}\nWaiting for confirmation..."
                send_telegram_message(msg)
                logger.info("PENDING LONG Setup Detected.")
                self.state = "PENDING_LONG"
                self.pending_setup_candle = snap
                return
                
            # --- SHORT SETUP RULES ---
            # Context: price near VAH, price below VWAP
            is_below_vwap = close < vwap
            is_near_vah = abs(close - vah) < (atr * 0.5)
            
            # Pressure: delta positive
            is_delta_very_positive = delta > abs(avg_delta * 1.5)
            if avg_delta == 0 and delta > 5.0:
                 is_delta_very_positive = True
            
            # Response: price cannot continue higher (close near low means absorption sell)
            is_close_near_low = (close - low) <= (atr * 0.2)
            
            if is_below_vwap and is_near_vah and is_delta_very_positive and is_close_near_low:
                msg = f"⏳ <b>PENDING SHORT</b>\nPrice: {close:.2f}\nVWAP: {vwap:.2f}\nVAH: {vah:.2f}\nDelta: {delta:.2f}\nWaiting for confirmation..."
                send_telegram_message(msg)
                logger.info("PENDING SHORT Setup Detected.")
                self.state = "PENDING_SHORT"
                self.pending_setup_candle = snap
                return
                
        elif self.state == "PENDING_LONG":
            # Confirmation: next candle bullish (breaks previous high)
            if close > self.pending_setup_candle['high']:
                # SL is 1 tick below the low of the absorption candle
                sl_price = self.pending_setup_candle['low'] - 0.1
                
                msg = f"🚀 <b>LONG CONFIRMED</b>\nPrice: {close:.2f}\nSL: {sl_price:.2f}\nExecuting Grid..."
                send_telegram_message(msg)
                logger.info("LONG CONFIRMED. Executing grid.")
                
                self.execute_grid(db, 'LONG', close, sl_price, atr, vwap)
            else:
                logger.info("LONG Setup Failed Confirmation. Back to scanning.")
            
            self.state = "SCANNING"
            self.pending_setup_candle = None
            
        elif self.state == "PENDING_SHORT":
            # Confirmation: next candle breaks low
            if close < self.pending_setup_candle['low']:
                # SL is 1 tick above the high of the absorption candle
                sl_price = self.pending_setup_candle['high'] + 0.1
                
                msg = f"📉 <b>SHORT CONFIRMED</b>\nPrice: {close:.2f}\nSL: {sl_price:.2f}\nExecuting Grid..."
                send_telegram_message(msg)
                logger.info("SHORT CONFIRMED. Executing grid.")
                
                self.execute_grid(db, 'SHORT', close, sl_price, atr, vwap)
            else:
                logger.info("SHORT Setup Failed Confirmation. Back to scanning.")
                
            self.state = "SCANNING"
            self.pending_setup_candle = None

# Global state machine instance
state_machine = TradingStateMachine()

def db_sync_loop(tracker: OrderFlowTracker, db):
    """Periodically evaluates the candle and saves snapshot."""
    while True:
        # Wait until the top of the next minute
        now = time.time()
        sleep_time = 60 - (now % 60)
        time.sleep(sleep_time)
        
        # Take snapshot at the minute boundary
        snap = tracker.get_snapshot()
        timestamp = int(time.time() * 1000)
        
        # Evaluate Advanced Strategy State Machine (including execution)
        state_machine.evaluate(tracker, db, snap)
        
        doc = {
            'symbol': SYMBOL,
            'timestamp': timestamp,
            'interval': '1m',
            'cvd': snap['cvd'],
            'delta': snap['delta'],
            'bidVolume': snap['bid_volume'],
            'askVolume': snap['ask_volume'],
            'totalVolume': snap['total_volume'],
            'pocPrice': snap['poc_price'],
            'open': snap['open'],
            'high': snap['high'],
            'low': snap['low'],
            'close': snap['close'],
            'imbalances': snap['imbalances']
        }
        
        if db is not None:
            try:
                db.order_flow_candles.insert_one(doc)
                logger.info(f"Saved 1m: Delta {snap['delta']:.2f} | Close: {snap['close']:.2f} | Imbalances: {len(snap['imbalances'])}")
            except Exception as e:
                logger.error(f"Failed to save to DB: {e}")
        else:
             logger.info(f"Evaluated 1m: Delta {snap['delta']:.2f} | Close: {snap['close']:.2f} | Imbalances: {len(snap['imbalances'])}")

        # Reset candle volumes but keep CVD running
        tracker.reset_candle()

def on_message(ws, message, tracker):
    try:
        data = json.loads(message)
        # Check if it's an aggTrade event
        if data.get('e') == 'aggTrade':
            price = float(data['p'])
            quantity = float(data['q'])
            is_buyer_maker = data['m']
            
            tracker.process_binance_trade(price, quantity, is_buyer_maker)
    except Exception as e:
        logger.error(f"Error processing message: {e}")

def on_error(ws, error):
    logger.error(f"WebSocket Error: {error}")

def on_close(ws, close_status_code, close_msg):
    logger.warning("WebSocket Closed")

def on_open(ws):
    logger.info(f"Connected to Binance WebSocket for {SYMBOL}...")

def main():
    logger.info(f"Initializing Binance Advanced Strategy Service for {SYMBOL}...")
    if CAPITAL_LIVE_ENABLED:
         logger.info("🔴 CAPITAL.COM LIVE TRADING ENABLED")
    else:
         logger.info("⚪ TRADING DISABLED (PAPER MODE ONLY)")
         
    tracker = OrderFlowTracker(tick_size=0.1, history_size=14)
    
    # Connect to MongoDB
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        mongo_client.server_info() # trigger exception if cannot connect
        db = mongo_client.get_database()
        db.order_flow_candles.create_index([('symbol', 1), ('timestamp', -1)], unique=True)
        logger.info("MongoDB connected.")
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}. Running without DB saving.")
        db = None
    
    # Start the periodic DB sync and evaluation thread
    eval_thread = threading.Thread(target=db_sync_loop, args=(tracker, db), daemon=True)
    eval_thread.start()

    # Setup Binance WebSocket
    stream_url = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@aggTrade"
    
    # Reconnection Loop
    while True:
        try:
            logger.info("Starting WebSocket connection...")
            ws = websocket.WebSocketApp(stream_url,
                                      on_open=on_open,
                                      on_message=lambda ws, msg: on_message(ws, msg, tracker),
                                      on_error=on_error,
                                      on_close=on_close)
                                      
            # ping_interval and ping_timeout help detect silent drops
            ws.run_forever(ping_interval=60, ping_timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE})
            
            logger.warning("WebSocket run_forever exited. Reconnecting in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket execution failed: {e}. Retrying in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    main()
