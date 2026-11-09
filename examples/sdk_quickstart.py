"""Check feature flags from a Python app with the flagpole SDK.

Run a server first (in another terminal):   flagpole serve --dev
Then:  FLAGPOLE_URL=http://127.0.0.1:8080 FLAGPOLE_CLIENT_TOKEN=<client token> python examples/sdk_quickstart.py
"""

import os

from flagpole.client import FlagpoleClient

url = os.environ.get("FLAGPOLE_URL", "http://127.0.0.1:8080")
token = os.environ["FLAGPOLE_CLIENT_TOKEN"]

with FlagpoleClient(url, token, refresh_interval=30) as flags:
    if flags.last_error:
        print(f"Couldn't reach flagpole ({flags.last_error}); using defaults.")

    users = [
        {"key": "user-1", "email": "sam@example.com"},
        {"key": "user-2", "country": "DE", "plan": "pro"},
        {"key": "user-3", "country": "US", "plan": "free"},
    ]
    for user in users:
        result = flags.evaluate("new-checkout", user, default=False)
        print(f"{user['key']}: new checkout {'ON ' if result.value else 'off'}  ({result.reason})")
