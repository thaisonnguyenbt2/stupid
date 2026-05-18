import os
import time
import threading
from dotenv import load_dotenv
from dxfeed_client import DxFeedClient
from indicators import OrderFlowTracker

from pymongo import MongoClient

# Load configuration
load_dotenv()
SYMBOL = os.getenv('DXFEED_SYMBOL', 'XAU/USD')
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/trading')

def db_sync_loop(tracker: OrderFlowTracker, db):
    """Periodically saves the ATAS-style order flow snapshot to MongoDB."""
    while True:
        # Wait until the top of the next minute
        now = time.time()
        sleep_time = 60 - (now % 60)
        time.sleep(sleep_time)
        
        # Take snapshot at the minute boundary
        snap = tracker.get_snapshot()
        timestamp = int(time.time() * 1000)
        
        doc = {
            'symbol': SYMBOL,
            'timestamp': timestamp,
            'interval': '1m',
            'cvd': snap['cvd'],
            'delta': snap['delta'],
            'bidVolume': snap['bid_volume'],
            'askVolume': snap['ask_volume'],
            'totalVolume': snap['total_volume'],
            'pocPrice': snap['poc_price']
        }
        
        try:
            db.order_flow_candles.insert_one(doc)
            print(f"[{time.strftime('%H:%M:%S')}] Saved Order Flow 1m: Delta {snap['delta']:.2f} | CVD {snap['cvd']:.2f}")
        except Exception as e:
            print(f"Failed to save to DB: {e}")
            
        # Reset candle volumes but keep CVD running
        tracker.reset_candle()

def main():
    print("Initializing dxFeed Ingestion Service...")
    client = DxFeedClient()
    tracker = OrderFlowTracker(tick_size=0.1) # 0.1 is usually good for XAU/USD
    
    # Connect to MongoDB
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client.get_database()
    
    # Ensure indexes
    db.order_flow_candles.create_index([('symbol', 1), ('timestamp', -1)], unique=True)
    
    # Start the periodic DB sync in a background thread
    db_thread = threading.Thread(target=db_sync_loop, args=(tracker, db), daemon=True)
    db_thread.start()

    print(f"Listening for tick data on {SYMBOL}...")
    try:
        # Stream Trade and Quote events to build accurate order flow
        for event in client.stream_events(symbols=[SYMBOL], event_types=['Trade', 'Quote']):
            # Usually dxFeed SSE returns lists of events per message
            if isinstance(event, list):
                for e in event:
                    # Very basic parsing based on standard dxFeed JSON structure
                    event_type = e.get('eventType', '')
                    
                    if event_type == 'Quote':
                        bid = e.get('bidPrice', 0.0)
                        ask = e.get('askPrice', 0.0)
                        tracker.update_quote(bid, ask)
                        
                    elif event_type == 'Trade':
                        price = e.get('price', 0.0)
                        size = e.get('size', 0.0)
                        if price > 0 and size > 0:
                            tracker.process_trade(price, size)
            
    except KeyboardInterrupt:
        print("\nShutting down dxFeed ingest.")
    except Exception as e:
        print(f"Fatal Error: {e}")

if __name__ == "__main__":
    main()
