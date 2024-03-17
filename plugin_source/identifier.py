# Create a unique identifier for each user to group stats
import uuid
import hashlib
import aqt

# We identify the user by the their ankiweb email. Since I don't want to store their email for privacy reasons, we hash it and use that as the identifier.
# If the user doesn't have an ankiweb account, we use their MAC address to identify them.
# We prefer to use the ankiweb email because it is more stable than the MAC address and it allows us to identify the user across multiple devices.
def get_user_hash():
    """Create a unique identifier for the user"""
    identifier = None
    try:
        identifier = aqt.mw.pm.profile["syncUser"]
    except KeyError:
        # Get the MAC address
        mac = uuid.getnode()
        # Convert the MAC address to a string
        identifier = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
    user_hash = hashlib.sha256()
    user_hash.update(identifier.encode('utf-8'))
    return user_hash.hexdigest()
