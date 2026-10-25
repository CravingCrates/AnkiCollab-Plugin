import os

if not os.environ.get("SKIP_INIT"):
    try:
        import aqt  # noqa: F401
    except ImportError:
        # Not inside Anki.
        pass
    else:
        from .main import *  # noqa: F401,F403
