# Create a unique identifier for each user to group stats
import uuid
import hashlib
import aqt
import json
import requests

from .var_defs import API_BASE_URL

# We identify the user by the their ankiweb email. Since I don't want to store their email for privacy reasons, we hash it and use that as the identifier.
# If the user doesn't have an ankiweb account, we use their MAC address to identify them.
# We prefer to use the ankiweb email because it is more stable than the MAC address and it allows us to identify the user across multiple devices.
def get_user_hash():
    """Create a unique identifier for the user"""
    identifier = None
    try:
        sync_user = aqt.mw.pm.profile["syncUser"]
        # Check if syncUser is not just an empty string
        if sync_user and sync_user.strip():
            identifier = sync_user.strip()
    except (KeyError, AttributeError, TypeError):
        pass  # Fall through to MAC address
    
    if not identifier:
        # Get the MAC address
        mac = uuid.getnode()
        # Convert the MAC address to a string
        if mac and mac != 0:
            identifier = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
    
    # Fallback if both syncUser and MAC address fail
    if not identifier:
        # Generate a random UUID as last resort
        identifier = str(uuid.uuid4())
    
    user_hash = hashlib.sha256()
    user_hash.update(identifier.encode('utf-8'))
    return user_hash.hexdigest()

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