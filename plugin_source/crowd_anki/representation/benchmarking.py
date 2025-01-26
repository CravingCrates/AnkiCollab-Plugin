from collections import defaultdict
import time
import functools
import logging
from pathlib import Path
import os

class BenchmarkStats:
    stats = defaultdict(lambda: {'total_time': 0, 'calls': 0})
    
    @staticmethod
    def record(func_name, duration):
        BenchmarkStats.stats[func_name]['total_time'] += duration
        BenchmarkStats.stats[func_name]['calls'] += 1
    
    @staticmethod
    def print_stats():
        for func, data in sorted(BenchmarkStats.stats.items(), 
                               key=lambda x: x[1]['total_time'], reverse=True):
            print(f"{func}: {data['total_time']:.2f}s total, {data['calls']} calls")

# Modify benchmark decorator
def benchmark(func):
    @functools.wraps(func) 
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        duration = end - start
        
        if args and hasattr(args[0], '__class__'):
            class_name = args[0].__class__.__name__
            func_name = f"{class_name}.{func.__name__}"
        else:
            func_name = func.__name__
            
        BenchmarkStats.record(func_name, duration)
        return result
    return wrapper