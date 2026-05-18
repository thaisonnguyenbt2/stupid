import os
import json
import requests
import sseclient
from urllib.parse import urlencode

class DxFeedClient:
    """Client to connect to dxFeed REST/SSE API and stream events."""
    
    def __init__(self, endpoint: str = None, token: str = None):
        self.endpoint = endpoint or os.getenv('DXFEED_ENDPOINT', 'https://tools.dxfeed.com/webservice/rest')
        self.token = token or os.getenv('DXFEED_TOKEN', '')
        self.session = requests.Session()
        if self.token:
            self.session.headers.update({'Authorization': f'Bearer {self.token}'})

    def stream_events(self, symbols: list, event_types: list = ['Trade', 'Quote']):
        """
        Connects to the SSE stream and yields events as they arrive.
        event_types can include: Trade, Quote, Profile, Order, TimeAndSale
        """
        params = {
            'events': ','.join(event_types),
            'symbols': ','.join(symbols)
        }
        
        url = f"{self.endpoint}/events.json?{urlencode(params)}"
        print(f"Connecting to dxFeed SSE stream: {url}")
        
        # We use stream=True and pass it to sseclient
        try:
            response = self.session.get(url, stream=True, headers={'Accept': 'text/event-stream'})
            response.raise_for_status()
            
            client = sseclient.SSEClient(response)
            for event in client.events():
                if event.data:
                    yield json.loads(event.data)
        except Exception as e:
            print(f"Error streaming from dxFeed: {e}")
            raise
