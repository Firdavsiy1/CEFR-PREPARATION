"""
Simple IP-based rate limiting using Django's cache framework.

No external dependencies required — works with LocMemCache (dev)
and Redis-backed cache (production).

Usage in views:
    from .ratelimit import check_rate_limit

    if request.method == 'POST':
        if check_rate_limit(request, 'register', limit=5, window=900):
            messages.error(request, 'Too many attempts. Please try again later.')
            return render(request, 'accounts/register.html', {'form': form})
"""

from django.core.cache import cache


def _get_client_ip(request) -> str:
    """Extract the real client IP, respecting common proxy headers."""
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def check_rate_limit(request, key_prefix: str, limit: int = 5, window: int = 900) -> bool:
    """
    Return True (limit exceeded) or False (allowed) and increment the counter.

    Args:
        request:    Current HttpRequest.
        key_prefix: Unique name for this endpoint/action (e.g. 'register').
        limit:      Maximum allowed POST attempts in the time window.
        window:     Time window in seconds (default 900 = 15 minutes).
    """
    ip = _get_client_ip(request)
    cache_key = f"rl:{key_prefix}:{ip}"
    count = cache.get(cache_key, 0)
    if count >= limit:
        return True
    # add_to_cache if key doesn't exist yet (race-safe increment)
    if not cache.add(cache_key, 1, timeout=window):
        cache.incr(cache_key)
    return False
