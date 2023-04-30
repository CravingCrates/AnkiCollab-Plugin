from threading import Thread

def run_function_in_thread(function, *args, **kwargs):
    # Create a new thread
    thread = Thread(target=function, args=args, kwargs=kwargs)
    thread.start()