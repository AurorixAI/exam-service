"""One long-lived ``httpx.AsyncClient`` per event loop and timeout instead of a new client (a new TCP connection) per call (EX-45).

Every staff request asks auth-service ``/auth/me`` and ``/auth/org/groups/{id}``; a client created and closed per call opens a
new connection (and a handshake) each time.  ``pooled_client`` is a drop-in for ``httpx.AsyncClient(timeout=...)`` in
``async with``: it hands out the shared client and does NOT close it on exit.

* One client per (event loop, class, timeout, redirects): connections of a client belong to the loop that opened them, and a
  loop that is gone (a test's ``asyncio.run``) takes its clients with it (``WeakKeyDictionary``).
* The shared client keeps NO cookies: a ``Set-Cookie`` answer to one person's call must never ride on the next person's request
  (the pool is shared by every teacher and student; a client that lived for one request could not leak, this one could).
* The class is part of the key: whoever swaps ``httpx.AsyncClient`` (a test double) gets a client of the swapped class.
* ``close_pools`` runs at shutdown on the serving loop.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import weakref
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

# Enough for one worker: staff calls are bounded by `staff_slots`/`heavy_slots`; keep-alive is short so a restarted auth-service is not held on a dead socket.
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=20.0)

_pools: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple, Any]]" = weakref.WeakKeyDictionary()


def _client_for(timeout: float, follow_redirects: bool) -> Any:
    loop = asyncio.get_running_loop()
    key = (httpx.AsyncClient, float(timeout), bool(follow_redirects))
    per_loop = _pools.setdefault(loop, {})
    client = per_loop.get(key)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects, limits=_LIMITS)
        # a jar that accepts nothing (``allowed_domains=[]`` refuses every domain): see the module note
        client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        per_loop[key] = client
    return client


@asynccontextmanager
async def pooled_client(*, timeout: float = 5.0, follow_redirects: bool = False) -> AsyncIterator[Any]:
    """``async with pooled_client(timeout=5.0) as client:`` - the shared client of this loop; leaving the block does not close it."""
    yield _client_for(timeout, follow_redirects)


async def close_pools() -> None:
    """Close the clients of the CURRENT loop (the application's shutdown hook)."""
    loop = asyncio.get_running_loop()
    for client in list((_pools.pop(loop, None) or {}).values()):
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()
