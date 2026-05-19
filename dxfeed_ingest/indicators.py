import collections
import threading

class SessionTracker:
    def __init__(self, tick_size=0.1):
        self.tick_size = tick_size
        self.lock = threading.RLock()
        
        self.cumulative_volume = 0.0
        self.cumulative_pv = 0.0  # Price * Volume
        
        self.volume_profile = collections.defaultdict(float)
        
    def add_trade(self, price: float, size: float):
        with self.lock:
            self.cumulative_volume += size
            self.cumulative_pv += price * size
            
            rounded_price = round(price / self.tick_size) * self.tick_size
            self.volume_profile[rounded_price] += size

    def get_vwap(self):
        with self.lock:
            if self.cumulative_volume == 0:
                return 0.0
            return self.cumulative_pv / self.cumulative_volume

    def get_value_area(self, percentage=0.70):
        """Calculate Value Area High (VAH), Value Area Low (VAL), and vPOC."""
        with self.lock:
            if not self.volume_profile:
                return {'vpoc': 0.0, 'vah': 0.0, 'val': 0.0}

            # Find vPOC
            vpoc = max(self.volume_profile.items(), key=lambda x: x[1])[0]
            
            # Calculate total volume
            total_vol = sum(self.volume_profile.values())
            target_vol = total_vol * percentage
            
            # Start Value Area at vPOC
            current_vol = self.volume_profile[vpoc]
            
            # Sort prices
            prices = sorted(self.volume_profile.keys())
            vpoc_idx = prices.index(vpoc)
            
            upper_idx = vpoc_idx + 1
            lower_idx = vpoc_idx - 1
            
            vah = vpoc
            val = vpoc
            
            # Expand up and down to capture 70% of volume
            while current_vol < target_vol:
                vol_up = self.volume_profile[prices[upper_idx]] if upper_idx < len(prices) else -1
                vol_down = self.volume_profile[prices[lower_idx]] if lower_idx >= 0 else -1
                
                if vol_up == -1 and vol_down == -1:
                    break
                    
                if vol_up > vol_down:
                    current_vol += vol_up
                    vah = prices[upper_idx]
                    upper_idx += 1
                elif vol_down > vol_up:
                    current_vol += vol_down
                    val = prices[lower_idx]
                    lower_idx -= 1
                else: # Tie
                    current_vol += vol_up + vol_down
                    vah = prices[upper_idx] if upper_idx < len(prices) else vah
                    val = prices[lower_idx] if lower_idx >= 0 else val
                    upper_idx += 1
                    lower_idx -= 1
                    
            return {'vpoc': vpoc, 'vah': vah, 'val': val}

    def reset_session(self):
        with self.lock:
            self.cumulative_volume = 0.0
            self.cumulative_pv = 0.0
            self.volume_profile.clear()


class OrderFlowTracker:
    """
    Tracks tick data and builds ATAS-style Order Flow indicators.
    Now includes historical moving averages (ATR, Avg Volume, Avg Delta).
    """
    def __init__(self, tick_size=0.1, history_size=14):
        self.tick_size = tick_size
        self.history_size = history_size
        self.lock = threading.RLock()
        
        self.session = SessionTracker(tick_size)
        
        # History (deque of dicts containing candle snapshots)
        self.history = collections.deque(maxlen=history_size)
        
        # Current Candle State
        self.current_cvd = 0.0
        self.bid_volume = 0.0
        self.ask_volume = 0.0
        self.open_price = 0.0
        self.close_price = 0.0
        self.high_price = 0.0
        self.low_price = float('inf')
        
        self.volume_profile = collections.defaultdict(lambda: {'bid': 0.0, 'ask': 0.0, 'total': 0.0})
        
        # Latest quotes
        self.best_bid = 0.0
        self.best_ask = 0.0

    def update_quote(self, bid_price: float, ask_price: float):
        if bid_price: self.best_bid = bid_price
        if ask_price: self.best_ask = ask_price

    def _update_price_extremes(self, price: float):
        if self.open_price == 0.0:
            self.open_price = price
        self.close_price = price
        if price > self.high_price:
            self.high_price = price
        if price < self.low_price:
            self.low_price = price

    def process_binance_trade(self, price: float, size: float, is_buyer_maker: bool):
        self.session.add_trade(price, size)
        
        with self.lock:
            self._update_price_extremes(price)
            is_buy = not is_buyer_maker
            
            if is_buy:
                self.ask_volume += size
                self.current_cvd += size
            else:
                self.bid_volume += size
                self.current_cvd -= size

            rounded_price = round(price / self.tick_size) * self.tick_size
            prof = self.volume_profile[rounded_price]
            prof['total'] += size
            if is_buy:
                prof['ask'] += size
            else:
                prof['bid'] += size

    def get_snapshot(self):
        with self.lock:
            poc_price = 0.0
            max_vol = 0.0
            for price, data in self.volume_profile.items():
                if data['total'] > max_vol:
                    max_vol = data['total']
                    poc_price = price
                    
            # Identify Imbalances (ratio > 3)
            imbalances = []
            for price, data in self.volume_profile.items():
                if data['ask'] > data['bid'] * 3 and data['ask'] > 0:
                    imbalances.append({'price': price, 'type': 'bullish'})
                elif data['bid'] > data['ask'] * 3 and data['bid'] > 0:
                    imbalances.append({'price': price, 'type': 'bearish'})
                    
            return {
                'cvd': self.current_cvd,
                'delta': self.ask_volume - self.bid_volume,
                'bid_volume': self.bid_volume,
                'ask_volume': self.ask_volume,
                'total_volume': self.bid_volume + self.ask_volume,
                'poc_price': poc_price,
                'imbalances': imbalances,
                'open': self.open_price,
                'high': self.high_price,
                'low': self.low_price if self.low_price != float('inf') else 0.0,
                'close': self.close_price
            }

    def reset_candle(self):
        """Reset volume and profile for the next candle period."""
        with self.lock:
            # Store the current state in history before resetting
            snap = self.get_snapshot()
            if snap['open'] != 0.0:  # Only save if there was actual trading
                self.history.append(snap)
                
            self.bid_volume = 0.0
            self.ask_volume = 0.0
            self.volume_profile.clear()
            
            self.open_price = 0.0
            self.close_price = 0.0
            self.high_price = 0.0
            self.low_price = float('inf')

    # --- Historical Aggregation Methods ---
    
    def get_avg_volume(self):
        with self.lock:
            if not self.history: return 0.0
            return sum(h['total_volume'] for h in self.history) / len(self.history)
            
    def get_avg_delta(self):
        with self.lock:
            if not self.history: return 0.0
            return sum(h['delta'] for h in self.history) / len(self.history)
            
    def get_atr(self):
        """Calculate Average True Range over the history window."""
        with self.lock:
            if len(self.history) < 2: return 0.0
            trs = []
            for i in range(1, len(self.history)):
                curr = self.history[i]
                prev = self.history[i-1]
                tr = max(curr['high'] - curr['low'], 
                         abs(curr['high'] - prev['close']), 
                         abs(curr['low'] - prev['close']))
                trs.append(tr)
            return sum(trs) / len(trs) if trs else 0.0
