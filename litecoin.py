import websocket
import json
import threading
import time
from typing import Callable, Optional

class LitecoinPriceManager:
    def __init__(self, on_price_update: Optional[Callable[[float], None]] = None):
        self.current_price = 0.0
        self.previous_price = 0.0
        self.on_price_update = on_price_update
        self.ws = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        
    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            # Handle Binance WebSocket message format
            if 'c' in data:  # Current price field from Binance ticker
                new_price = float(data['c'])
                self.update_price(new_price)
                # print(f"Price: ${new_price:.2f}, Change: {data.get('P', '0')}%")
        except Exception as e:
            print(f"Error parsing message: {e}")
    
    def on_error(self, ws, error):
        print(f"WebSocket error: {error}")
        self.is_connected = False
    
    def on_close(self, ws, close_status_code, close_msg):
        print("WebSocket connection closed")
        self.is_connected = False
        self.attempt_reconnect()
    
    def on_open(self, ws):
        print("WebSocket connection opened")
        self.is_connected = True
        self.reconnect_attempts = 0
        # No need to subscribe - Binance ticker stream is already active
    
    def update_price(self, new_price: float):
        self.previous_price = self.current_price
        self.current_price = new_price
        
        if self.on_price_update:
            self.on_price_update(new_price)
    
    def start(self):
        """Start the WebSocket connection"""
        self.ws = websocket.WebSocketApp(
            "wss://stream.binance.com:9443/ws/ltcusdt@ticker",
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        
        # Run in a separate thread
        self.ws_thread = threading.Thread(target=self.ws.run_forever)
        self.ws_thread.daemon = True
        self.ws_thread.start()
    
    def stop(self):
        """Stop the WebSocket connection"""
        if self.ws:
            self.ws.close()
        self.is_connected = False
    
    def attempt_reconnect(self):
        """Attempt to reconnect with exponential backoff"""
        if self.reconnect_attempts < self.max_reconnect_attempts:
            self.reconnect_attempts += 1
            wait_time = 2 ** self.reconnect_attempts
            print(f"Reconnecting in {wait_time} seconds (attempt {self.reconnect_attempts})")
            time.sleep(wait_time)
            self.start()
    
    def get_current_price(self) -> float:
        """Get the current Litecoin price"""
        return self.current_price
    
    def get_price_change(self) -> float:
        """Get the price change from previous update"""
        return self.current_price - self.previous_price
    
    def get_price_change_percent(self) -> float:
        """Get the price change percentage"""
        if self.previous_price == 0:
            return 0.0
        return ((self.current_price - self.previous_price) / self.previous_price) * 100
