import os
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from services.db_service import get_db_path
from services.mysql_init import init_mysql_schema


def init_sources_db():
    start_time = time.time()
    db_path = get_db_path("sources_db")
    raise RuntimeError(f"SQLite init_sources_db is disabled. Configure MySQL (DATABASE_URL). db_path={db_path}")

    elapsed = time.time() - start_time
    print(f"Sources database initialized in {elapsed:.3f}s")


def init_tracking_db():
    start_time = time.time()
    db_path = get_db_path("tracking_db")
    raise RuntimeError(f"SQLite init_tracking_db is disabled. Configure MySQL (DATABASE_URL). db_path={db_path}")
    elapsed = time.time() - start_time
    print(f"Tracking database initialized in {elapsed:.3f}s")


def init_podcasts_db():
    start_time = time.time()
    db_path = get_db_path("podcasts_db")
    raise RuntimeError(f"SQLite init_podcasts_db is disabled. Configure MySQL (DATABASE_URL). db_path={db_path}")
    elapsed = time.time() - start_time
    print(f"Podcasts database initialized in {elapsed:.3f}s")


def init_tasks_db():
    start_time = time.time()
    db_path = get_db_path("tasks_db")
    raise RuntimeError(f"SQLite init_tasks_db is disabled. Configure MySQL (DATABASE_URL). db_path={db_path}")
    elapsed = time.time() - start_time
    print(f"Tasks database initialized in {elapsed:.3f}s")


async def init_agent_session_db():
    """Initialize the agent session database. auto generated"""
    pass


def init_internal_sessions_db():
    start_time = time.time()
    db_path = get_db_path("internal_sessions_db")
    raise RuntimeError(f"SQLite init_internal_sessions_db is disabled. Configure MySQL (DATABASE_URL). db_path={db_path}")
    elapsed = time.time() - start_time
    print(f"Internal sessions database initialized in {elapsed:.3f}s")


def init_social_media_db():
    start_time = time.time()
    db_path = get_db_path("social_media_db")
    raise RuntimeError(f"SQLite init_social_media_db is disabled. Configure MySQL (DATABASE_URL). db_path={db_path}")
    elapsed = time.time() - start_time
    print(f"Social media database initialized in {elapsed:.3f}s")


async def init_databases():
    total_start = time.time()
    print("Initializing all databases...")
    if not os.environ.get("DATABASE_URL", "").startswith(("mysql://", "mysql+pymysql://")):
        raise RuntimeError("SQLite is disabled. Please set DATABASE_URL to a MySQL URL.")

    init_mysql_schema()
    total_elapsed = time.time() - total_start
    print(f"MySQL schema initialized in {total_elapsed:.3f}s")
    return

    for db_name in ["sources_db", "tracking_db", "podcasts_db", "tasks_db"]:
        db_path = get_db_path(db_name)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = [
            loop.run_in_executor(executor, init_sources_db),
            loop.run_in_executor(executor, init_tracking_db),
            loop.run_in_executor(executor, init_podcasts_db),
            loop.run_in_executor(executor, init_tasks_db),
            loop.run_in_executor(executor, init_internal_sessions_db),
            loop.run_in_executor(executor, init_social_media_db),
        ]
        await asyncio.gather(*tasks)
    total_elapsed = time.time() - total_start
    print(f"All databases initialized in {total_elapsed:.3f}s")


def init_all_databases():
    asyncio.run(init_databases())
