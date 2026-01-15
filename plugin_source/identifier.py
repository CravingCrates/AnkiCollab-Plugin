# Create a unique identifier for each user to group stats
import json
import requests

from .auth_manager import auth_manager
from .var_defs import API_BASE_URL

# We identify the user by a hashed version of their username returned by the backend.
# This is derived from the token without storing any email or MAC address.
def get_user_hash():
    """Get the backend-provided hashed username for the current token."""
    token = auth_manager.get_token()
    if not token:
        return None

    try:
        response = requests.post(
            f"{API_BASE_URL}/GetUserHashFromToken",
            json={"token": token},
            timeout=5,
        )
    except Exception:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
    except json.JSONDecodeError:
        return None

    return None

# purely aesthetic function to increase the counter on the website
def subscribe_to_deck(deck_hash):
    user_hash = get_user_hash()
    if not user_hash:
        return False
    payload = {
            'deck_hash': deck_hash,
            'user_hash': user_hash
        }
    print(payload)
    response = requests.post(f"{API_BASE_URL}/AddSubscription", json=payload, timeout=5)
    if response.status_code == 200:
        return True
    else:
        return False
    
def unsubscribe_from_deck(deck_hash):
    user_hash = get_user_hash()
    if not user_hash:
        return False
    payload = {
            'deck_hash': deck_hash,
            'user_hash': user_hash
        }
    response = requests.post(f"{API_BASE_URL}/RemoveSubscription", json=payload, timeout=5)
    if response.status_code == 200:
        return True
    else:
        return False