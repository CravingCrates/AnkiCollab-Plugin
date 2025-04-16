from threading import Thread
import asyncio
import sys
import traceback
import logging
import aqt
import anki
from aqt import mw


logger = logging.getLogger("ankicollab")

def thread_exception_handler(args):
    """Handle uncaught exceptions in threads"""
    exc_type, exc_value, exc_traceback = args
    logger.error(f"Uncaught exception in thread: {exc_type.__name__}: {exc_value}")
    logger.error("".join(traceback.format_tb(exc_traceback)))
    
    aqt.mw.taskman.run_on_main(
            lambda: aqt.utils.showWarning(f"An Error has occurred! That's not good!",
                title="AnkiCollab Error",
                parent=mw
            )
        )

def run_function_in_thread(function, *args, **kwargs):
    """Run a function in a separate thread with proper exception handling"""
    def wrapped_function():
        try:
            return function(*args, **kwargs)
        except Exception as e:
            logger.error(f"Exception in thread function {function.__name__}: {str(e)}")
            logger.error(traceback.format_exc())
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.showWarning(f"An Error has occurred! That's not good!",
                    title="AnkiCollab Error",
                    parent=mw
                )
            )
            raise  # Re-raise to allow system exception hooks to work
    
    thread = Thread(target=wrapped_function)
    thread.daemon = True  # Make thread terminate when main thread exits
    thread.start()
    return thread

def run_async_function_in_thread(async_function, *args, **kwargs):
    """Run an async function in a separate thread with its own event loop and exception handling"""
    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(async_function(*args, **kwargs))
            return result
        except Exception as e:
            logger.error(f"Exception in async thread function {async_function.__name__}: {str(e)}")
            logger.error(traceback.format_exc())
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.showWarning(f"An Error has occurred! That's not good!",
                    title="AnkiCollab Error",
                    parent=mw
                )
            )            
            raise  # Re-raise to allow system exception hooks to work
        finally:
            loop.close()
    
    thread = Thread(target=run_async)
    thread.daemon = True  # Make thread terminate when main thread exits
    thread.start()
    return thread

def sync_run_async(async_function, *args, **kwargs):
    """Run an async function synchronously from a sync context with proper exception handling"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(async_function(*args, **kwargs))
    except Exception as e:
        logger.error(f"Exception in sync_run_async for {async_function.__name__}: {str(e)}")
        logger.error(traceback.format_exc())
        raise  # Re-raise in the calling context
    finally:
        loop.close()

# Install exception handler for threads
sys.excepthook = thread_exception_handler