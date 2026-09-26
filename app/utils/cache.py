import time
from functools import wraps
from typing import Callable, Any

# Simple TTL In-Memory Response Cache for Repeated Query Endpoints
_CACHE_STORE = {}

def cache_response(ttl_seconds: int = 30):
    def decorator(func: Callable[..., Any]):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Formulate cache key based on function name, user ID, and stringified kwargs
            user = kwargs.get("current_user")
            user_id = str(user.id) if user and hasattr(user, "id") else "public"
            
            # Extract primitive kwargs
            clean_kwargs = {k: v for k, v in kwargs.items() if k not in ["db", "current_user"]}
            cache_key = f"{func.__name__}:{user_id}:{sorted(clean_kwargs.items())}"
            
            now = time.time()
            if cache_key in _CACHE_STORE:
                cached_time, cached_val = _CACHE_STORE[cache_key]
                if now - cached_time < ttl_seconds:
                    return cached_val

            result = func(*args, **kwargs)
            _CACHE_STORE[cache_key] = (now, result)
            return result
        return wrapper
    return decorator

def invalidate_user_cache(user_id: str):
    """Invalidates cached entries for a given user after new file processing."""
    to_delete = [k for k in _CACHE_STORE.keys() if f":{user_id}:" in k]
    for k in to_delete:
        _CACHE_STORE.pop(k, None)

