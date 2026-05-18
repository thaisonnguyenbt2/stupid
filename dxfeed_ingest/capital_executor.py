import os
import time
import requests
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# Capital.com Live Trading configuration
CAPITAL_LIVE_ENABLED = True
CAPITAL_DEMO = True  # Hardcoded to DEMO account for safety
CAPITAL_API_KEY = os.getenv('CAPITAL_API_KEY_DEMO', '') if CAPITAL_DEMO else os.getenv('CAPITAL_API_KEY_LIVE', '')
CAPITAL_API_PASSWORD = os.getenv('CAPITAL_API_PASSWORD', '')
CAPITAL_EMAIL = os.getenv('CAPITAL_EMAIL', '')
CAPITAL_BASE_URL = 'https://demo-api-capital.backend-capital.com/api/v1' if CAPITAL_DEMO \
    else 'https://api-capital.backend-capital.com/api/v1'
CAPITAL_EPIC = 'GOLD'  # XAU/USD on Capital.com

class CapitalClient:
    """Thin REST client for Capital.com trade execution."""

    def __init__(self):
        self.cst = None
        self.security_token = None
        self.last_session_time = 0
        self.max_positions = 6

    def _ensure_session(self):
        """Create or refresh session (expires every 10 min)."""
        if time.time() - self.last_session_time > 540:  # Refresh at 9 min
            try:
                resp = requests.post(f"{CAPITAL_BASE_URL}/session",
                    headers={
                        'X-CAP-API-KEY': CAPITAL_API_KEY,
                        'Content-Type': 'application/json',
                    },
                    json={
                        'identifier': CAPITAL_EMAIL,
                        'password': CAPITAL_API_PASSWORD,
                        'encryptedPassword': False,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    self.cst = resp.headers.get('CST')
                    self.security_token = resp.headers.get('X-SECURITY-TOKEN')
                    self.last_session_time = time.time()
                    print(f"[Capital] ✅ Session created ({'DEMO' if CAPITAL_DEMO else 'LIVE'})")
                else:
                    print(f"[Capital] ❌ Session failed: HTTP {resp.status_code} — {resp.text[:200]}")
            except Exception as e:
                print(f"[Capital] ❌ Session error: {e}")

    @property
    def headers(self):
        self._ensure_session()
        return {
            'X-CAP-API-KEY': CAPITAL_API_KEY,
            'CST': self.cst or '',
            'X-SECURITY-TOKEN': self.security_token or '',
            'Content-Type': 'application/json',
        }

    def open_trade(self, direction: str, lot_size: float, tp: float, sl: float,
                   strategy: str = '', slot: str = '') -> dict:
        """Open a real trade on Capital.com. Returns result dict or error."""
        if not self.cst:
            self._ensure_session()
        if not self.cst:
            return {'error': 'No session'}

        try:
            # Safety: check position count
            pos_resp = requests.get(f"{CAPITAL_BASE_URL}/positions", headers=self.headers, timeout=10)
            if pos_resp.status_code == 200:
                positions = pos_resp.json().get('positions', [])
                if len(positions) >= self.max_positions:
                    print(f"[Capital] ⛔ Max positions ({self.max_positions}) reached — skipping")
                    return {'error': f'Max positions ({self.max_positions}) reached'}

            # Execute
            order_data = {
                'epic': CAPITAL_EPIC,
                'direction': 'BUY' if direction == 'LONG' else 'SELL',
                'size': lot_size,
                'guaranteedStop': False,
                'stopLevel': round(sl, 2),
                'profitLevel': round(tp, 2),
            }
            resp = requests.post(f"{CAPITAL_BASE_URL}/positions",
                headers=self.headers, json=order_data, timeout=10)
            result = resp.json()

            if resp.status_code in (200, 201):
                deal_ref = result.get('dealReference', '')
                # Confirm the deal
                if deal_ref:
                    confirm_resp = requests.get(f"{CAPITAL_BASE_URL}/confirms/{deal_ref}",
                        headers=self.headers, timeout=10)
                    if confirm_resp.status_code == 200:
                        confirm = confirm_resp.json()
                        status = confirm.get('dealStatus', 'UNKNOWN')
                        print(f"[Capital] ✅ {direction} {lot_size} GOLD | Deal: {deal_ref} | Status: {status}")
                        return {'dealReference': deal_ref, 'status': status, 'confirm': confirm}
                    else:
                        print(f"[Capital] ⚠️ Confirm failed: {confirm_resp.text[:200]}")

                print(f"[Capital] ✅ Order sent: {deal_ref}")
                return {'dealReference': deal_ref, 'result': result}
            else:
                print(f"[Capital] ❌ Order failed: HTTP {resp.status_code} — {resp.text[:200]}")
                return {'error': resp.text[:200]}

        except Exception as e:
            print(f"[Capital] ❌ Trade error: {e}")
            return {'error': str(e)}

    def get_positions(self) -> list:
        """Get all open positions."""
        try:
            resp = requests.get(f"{CAPITAL_BASE_URL}/positions", headers=self.headers, timeout=10)
            if resp.status_code == 200:
                return resp.json().get('positions', [])
        except Exception as e:
            print(f"[Capital] ❌ Get positions error: {e}")
        return []
