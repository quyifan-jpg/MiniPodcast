"""
Redis-backed mutex for the shared Playwright persistent profile (USER_DATA_DIR).

Why Redis:
- scheduler launches social scrapers as separate subprocesses
- lock must work across process boundaries
- we also add TTL + watchdog renewal to avoid stale locks

Env:
  SOCIAL_BROWSER_LOCK_TIMEOUT_SEC    wait time for acquiring lock (default 7200)
  SOCIAL_BROWSER_LOCK_TTL_SEC        lock TTL before renew (default 120)
  SOCIAL_BROWSER_LOCK_RENEW_SEC      renew interval (default TTL/2)
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager

import redis

LOCK_KEY = "lock:social_browser_profile"
LOCK_INFO_KEY = "lock_info:social_browser_profile"

_RELEASE_LOCK_LUA = """
local lock_value = redis.call('GET', KEYS[1])
if lock_value == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('DEL', KEYS[2])
  return 1
else
  return 0
end
"""

_RENEW_LOCK_LUA = """
local lock_value = redis.call('GET', KEYS[1])
if lock_value == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
  redis.call('EXPIRE', KEYS[2], tonumber(ARGV[3]))
  return 1
else
  return 0
end
"""


def _build_redis_client() -> redis.Redis:
    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    db = int(os.environ.get("REDIS_DB", "0")) + 2
    username = os.environ.get("REDIS_USERNAME", None)
    password = os.environ.get("REDIS_PASSWORD", None)
    if username and password:
        return redis.Redis(host=host, port=port, db=db, username=username, password=password, decode_responses=True)
    if password:
        return redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _store_lock_info(client: redis.Redis, owner_token: str, ttl_sec: int) -> None:
    info = {
        "owner": owner_token,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "timestamp": time.time(),
    }
    client.set(LOCK_INFO_KEY, json.dumps(info), ex=ttl_sec)


@contextmanager
def social_browser_profile_lock(timeout_sec: int | None = None, poll_interval: float = 0.25):
    """
    Acquire an exclusive Redis lock before opening launch_persistent_context.
    Blocks until acquired or timeout_sec is exceeded.
    """
    if timeout_sec is None:
        timeout_sec = int(os.environ.get("SOCIAL_BROWSER_LOCK_TIMEOUT_SEC", "7200"))
    ttl_sec = int(os.environ.get("SOCIAL_BROWSER_LOCK_TTL_SEC", "120"))
    renew_sec = int(os.environ.get("SOCIAL_BROWSER_LOCK_RENEW_SEC", str(max(1, ttl_sec // 2))))

    client = _build_redis_client()
    owner_token = str(uuid.uuid4())
    start = time.monotonic()
    warned = False
    acquired = False

    while True:
        acquired = bool(client.set(LOCK_KEY, owner_token, nx=True, ex=ttl_sec))
        if acquired:
            _store_lock_info(client, owner_token, ttl_sec)
            break
        if not warned:
            print(f"[social browser] Another process holds Redis profile lock; waiting (timeout {timeout_sec}s)...")
            warned = True
        if time.monotonic() - start > timeout_sec:
            raise TimeoutError(
                f"Could not acquire Redis social browser profile lock within {timeout_sec}s. "
                "Stop other scrapers or session setup using the same profile."
            )
        time.sleep(poll_interval)

    stop_renew_event = threading.Event()

    def _renew_loop() -> None:
        while not stop_renew_event.is_set():
            try:
                ok = client.eval(_RENEW_LOCK_LUA, 2, LOCK_KEY, LOCK_INFO_KEY, owner_token, ttl_sec, ttl_sec)
                if ok:
                    _store_lock_info(client, owner_token, ttl_sec)
            except Exception as e:
                print(f"[social browser] Redis lock renew error: {e}")
            stop_renew_event.wait(renew_sec)

    renew_thread = threading.Thread(target=_renew_loop, daemon=True)
    renew_thread.start()
    try:
        yield
    finally:
        stop_renew_event.set()
        renew_thread.join(timeout=2)
        try:
            client.eval(_RELEASE_LOCK_LUA, 2, LOCK_KEY, LOCK_INFO_KEY, owner_token)
        except Exception as e:
            print(f"[social browser] Redis lock release error: {e}")
