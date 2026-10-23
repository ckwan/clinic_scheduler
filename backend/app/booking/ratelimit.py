"""A small per client limit on the public booking endpoints.

In memory and per process, which is fine for one API container. With several, put the limit at
the proxy or in Redis. The per link attempt limit in service.py is the real brake on guessing.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

WINDOW_S = 60
MAX_REQUESTS = 60

_hits: dict[str, deque] = defaultdict(deque)


def limit(request: Request) -> None:
    # nginx sets X-Real-IP to the caller; without it every visitor would share the proxy's address.
    client = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    hits = _hits[client]
    while hits and now - hits[0] > WINDOW_S:
        hits.popleft()
    if len(hits) >= MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute.")
    hits.append(now)


def reset() -> None:
    _hits.clear()
