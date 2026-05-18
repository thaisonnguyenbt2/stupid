import os
import requests
import json
from dotenv import load_dotenv

# Load configuration from .env file
load_dotenv()

# Required variables
DXFEED_RETAIL_SERVER = os.getenv('DXFEED_RETAIL_SERVER', 'demo') # e.g., 'tools' or your specific server prefix
DXFEED_RETAIL_API_KEY = os.getenv('DXFEED_RETAIL_API_KEY', '') # The application API Key
DXFEED_LOGIN = os.getenv('DXFEED_LOGIN', '')
DXFEED_PASSWORD = os.getenv('DXFEED_PASSWORD', '')

def get_dxfeed_token():
    if not all([DXFEED_RETAIL_SERVER, DXFEED_RETAIL_API_KEY, DXFEED_LOGIN, DXFEED_PASSWORD]):
        print("Error: Missing required environment variables.")
        print("Please ensure DXFEED_RETAIL_SERVER, DXFEED_RETAIL_API_KEY, DXFEED_LOGIN, and DXFEED_PASSWORD are set in your .env file.")
        return None

    # Construct the URL as per documentation
    url = f"https://{DXFEED_RETAIL_SERVER}.get.dxfeed.com/api/v1/token"
    
    headers = {
        'Authorization': f'Bearer {DXFEED_RETAIL_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    payload = {
        "login": DXFEED_LOGIN,
        "password": DXFEED_PASSWORD
    }
    
    print(f"Requesting token from: {url}")
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        
        data = response.json()
        
        if data.get('status') == 'OK':
            token = data.get('token')
            print("\n✅ Successfully retrieved dxFeed token!")
            print(f"Token: {token}")
            print("\nAdd this token to your .env file as DXFEED_TOKEN to use the dxfeed_ingest module.")
            return token
        else:
            print(f"\n❌ Error retrieving token. Status: {data.get('status')}")
            print(f"Reason: {data.get('reason')}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"\n❌ Request failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response Content: {e.response.text}")
        return None

if __name__ == "__main__":
    get_dxfeed_token()
