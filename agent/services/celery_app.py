from celery import Celery, Task
import redis
import os
import time
import json
import uuid
import threading
from dotenv import load_dotenv


# Load .env file from miniblog directory or parent directory
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_DB = int(os.environ.get("REDIS_DB", 0))
REDIS_USERNAME = os.environ.get("REDIS_USERNAME", None)
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", None)
REDIS_LOCK_EXP_TIME_SEC = 60 * 10
REDIS_LOCK_INFO_EXP_TIME_SEC = 60 * 15
STALE_LOCK_THRESHOLD_SEC = 60 * 15

# Build Redis connection URL
if REDIS_USERNAME and REDIS_PASSWORD:
    redis_url = f"redis://{REDIS_USERNAME}:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB + 1, username=REDIS_USERNAME, password=REDIS_PASSWORD)
elif REDIS_PASSWORD:
    redis_url = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB + 1, password=REDIS_PASSWORD)
else:
    redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB + 1)

# Debug: Print Redis connection info (without password)
if REDIS_USERNAME or REDIS_PASSWORD:
    print(
        f"[Redis Config] Using authenticated connection: redis://{REDIS_USERNAME or ''}:{'***' if REDIS_PASSWORD else ''}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
    )
else:
    print(f"[Redis Config] Using unauthenticated connection: {redis_url}")

app = Celery("miniblog_tasks", broker=redis_url, backend=redis_url)

app.conf.update(
    result_expires=60 * 2,
    task_track_started=True,
    worker_concurrency=2,
    task_acks_late=True,
    task_time_limit=600,
    task_soft_time_limit=540,
)

# Release lock safely: only delete if the current lock value matches our owner token.
# Also delete lock_info:{session_id} to avoid stale UI/task_id reads.
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

# Renew lock TTL only if we still own it.
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


class SessionLockedTask(Task):
    def __call__(self, *args, **kwargs):
        session_id = args[0] if args else kwargs.get("session_id")
        if not session_id:
            return super().__call__(*args, **kwargs)

        lock_key = f"lock:{session_id}"
        lock_info_key = f"lock_info:{session_id}"
        owner_token = str(uuid.uuid4())
        lock_info = redis_client.get(lock_info_key)
        if lock_info:
            try:
                lock_data = json.loads(lock_info.decode("utf-8"))
                lock_time = lock_data.get("timestamp", 0)
                if time.time() - lock_time > STALE_LOCK_THRESHOLD_SEC:
                    redis_client.delete(lock_key)
                    redis_client.delete(lock_info_key)
            except (ValueError, TypeError) as e:
                print(f"Error checking lock time: {e}")

        # Owner token is stored as the lock value so we can safely release via Lua.
        acquired = redis_client.set(lock_key, owner_token, nx=True, ex=REDIS_LOCK_EXP_TIME_SEC)
        if acquired:
            # Fencing token helps ordering if you later want to validate stale results.
            fence_token = redis_client.incr(f"lock_fence:{session_id}")
            lock_data = {
                "timestamp": time.time(),
                "task_id": self.request.id if hasattr(self, "request") else None,
                "owner": owner_token,
                "fence": fence_token,
            }
            redis_client.set(lock_info_key, json.dumps(lock_data), ex=REDIS_LOCK_INFO_EXP_TIME_SEC)

        if not acquired:
            return {
                "error": "Session busy",
                "response": "This session is already processing a message. Please wait.",
                "session_id": session_id,
                "stage": "busy",
                "session_state": "{}",
                "is_processing": True,
                "process_type": "chat",
            }

        # Watchdog: keep lock alive while the Celery task is running.
        stop_renew_event = threading.Event()
        renew_interval_sec = max(1, REDIS_LOCK_EXP_TIME_SEC // 2)

        def _renew_loop():
            while not stop_renew_event.is_set():
                try:
                    ok = redis_client.eval(
                        _RENEW_LOCK_LUA,
                        2,
                        lock_key,
                        lock_info_key,
                        owner_token,
                        REDIS_LOCK_EXP_TIME_SEC,
                        REDIS_LOCK_INFO_EXP_TIME_SEC,
                    )
                    if ok:
                        # Refresh lock_info.timestamp to avoid stale-lock mis-detection.
                        lock_info_now = redis_client.get(lock_info_key)
                        if lock_info_now:
                            try:
                                lock_data_now = json.loads(lock_info_now.decode("utf-8"))
                                if lock_data_now.get("owner") == owner_token:
                                    lock_data_now["timestamp"] = time.time()
                                    redis_client.set(
                                        lock_info_key,
                                        json.dumps(lock_data_now),
                                        ex=REDIS_LOCK_INFO_EXP_TIME_SEC,
                                    )
                            except (ValueError, TypeError):
                                # Best-effort only; if parsing fails we still renewed TTL above.
                                pass
                except Exception as e:
                    # Renewal failure shouldn't crash the task; stale lock cleanup will handle it.
                    print(f"Error renewing lock for session {session_id}: {e}")

                stop_renew_event.wait(renew_interval_sec)

        renew_thread = None
        if acquired:
            renew_thread = threading.Thread(target=_renew_loop, daemon=True)
            renew_thread.start()

        try:
            return super().__call__(*args, **kwargs)
        finally:
            if acquired:
                try:
                    stop_renew_event.set()
                    if renew_thread:
                        renew_thread.join(timeout=2)
                except Exception:
                    pass
                # Lua ensures only the owner that created the lock can delete it.
                try:
                    redis_client.eval(_RELEASE_LOCK_LUA, 2, lock_key, lock_info_key, owner_token)
                except Exception as e:
                    print(f"Error releasing lock via Lua: {e}")
