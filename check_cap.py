import requests, os
from dotenv import load_dotenv
load_dotenv(".env")

api_key = os.getenv("CAPITAL_API_KEY_DEMO")
pw = os.getenv("CAPITAL_API_PASSWORD")
email = os.getenv("CAPITAL_EMAIL")
base_url = "https://demo-api-capital.backend-capital.com/api/v1"

resp = requests.post(f"{base_url}/session", headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"}, json={"identifier": email, "password": pw})
print(resp.status_code, resp.text)
