from celery import Celery, Task
from kombu import Queue
import redis
import os
import time
import json
import uuid
import threading
from dotenv import load_dotenv
from loguru import logger

# Load .env file from miniblog directory or parent directory
env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=env_path)

# Use centralized config (import after dotenv so env vars are loaded)
from core.config import settings  # noqa: E402

REDIS_LOCK_EXP_TIME_SEC = 60 * 10
REDIS_LOCK_INFO_EXP_TIME_SEC = 60 * 15
STALE_LOCK_THRESHOLD_SEC = 60 * 15

# Build Redis connection URL and client from settings
redis_url = settings.redis_url
redis_client = redis.Redis(**{**settings.redis_kwargs, "db": settings.redis_db + 1})

logger.info(
    "[Celery] Redis broker: redis://***@{host}:{port}/{db}",
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
)

app = Celery("miniblog_tasks", broker=redis_url, backend=redis_url)

app.conf.update(
    result_expires=60 * 2,
    task_track_started=True,
    worker_concurrency=settings.celery_worker_concurrency,
    task_acks_late=True,
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
)

# ── Queue isolation (equivalent to CAgent's 9 named thread pool executors) ────
# Separate queues prevent audio/crawl tasks from starving AI chat tasks.
app.conf.task_queues = (
    Queue("agent"),    # AI conversation tasks — highest priority
    Queue("crawl"),    # Web crawling and article ingestion
    Queue("media"),    # Audio/image generation — resource-intensive
    Queue("default"),  # Catch-all for unrouted tasks
)
app.conf.task_default_queue = "default"
app.conf.task_routes = {
    "services.celery_tasks.agent_chat": {"queue": "agent"},
    "services.celery_tasks_vector.embed_article": {"queue": "crawl"},
    "services.celery_tasks_vector.index_pending_batch": {"queue": "crawl"},
    # Add routing as new task types are created:
    # "services.celery_tasks.generate_audio_*": {"queue": "media"},
    # "services.celery_tasks.generate_image_*": {"queue": "media"},
}

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
                logger.warning("Error checking lock time: {e}", e=str(e))

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
                    logger.warning("Error renewing lock for session {sid}: {e}", sid=session_id, e=str(e))

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
                    logger.error("Error releasing lock via Lua: {e}", e=str(e))
