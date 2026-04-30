# Create a unique identifier for each user to group stats
import json

from .auth_manager import auth_manager
from .api_client import api_client

# We identify the user by a hashed version of their username returned by the backend.
# This is derived from the token without storing any email or MAC address.
def get_user_hash():
    """Get the backend-provided hashed username for the current token."""
    token = auth_manager.get_token()
    if not token:
        return None

    try:
        response = api_client.post_empty("/GetUserHashFromToken")
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
    token = auth_manager.get_token()
    if not token:
        return False
    payload = {'deck_hash': deck_hash}
    response = api_client.post_json("/AddSubscription", payload, timeout=5)
    if response.status_code == 200:
        return True
    else:
        return False
    
def unsubscribe_from_deck(deck_hash):
    token = auth_manager.get_token()
    if not token:
        return False
    payload = {'deck_hash': deck_hash}
    response = api_client.post_json("/RemoveSubscription", payload, timeout=5)
    if response.status_code == 200:
        return True
    else:
        return False