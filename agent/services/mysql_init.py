import os
from db.connection import db_connection


MYSQL_TABLE_DDLS = [
    """
    CREATE TABLE IF NOT EXISTS sources (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL,
        description TEXT,
        url TEXT,
        is_active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(255) UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_categories (
        source_id BIGINT NOT NULL,
        category_id BIGINT NOT NULL,
        PRIMARY KEY (source_id, category_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_feeds (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        source_id BIGINT,
        feed_url TEXT,
        feed_type VARCHAR(64),
        is_active BOOLEAN DEFAULT TRUE,
        last_crawled TIMESTAMP NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_source_feeds_feed_url (feed_url(255))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feed_tracking (
        feed_id BIGINT PRIMARY KEY,
        source_id BIGINT,
        feed_url TEXT,
        last_processed TIMESTAMP NULL,
        last_etag TEXT,
        last_modified TEXT,
        entry_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feed_entries (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        feed_id BIGINT,
        source_id BIGINT,
        entry_id TEXT,
        title TEXT,
        link TEXT,
        published_date TIMESTAMP NULL,
        content LONGTEXT,
        summary LONGTEXT,
        crawl_status VARCHAR(32) DEFAULT 'pending',
        crawl_attempts INT DEFAULT 0,
        processed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_feed_entries_link (link(255))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crawled_articles (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        entry_id BIGINT,
        source_id BIGINT,
        feed_id BIGINT,
        title TEXT,
        url TEXT,
        published_date TIMESTAMP NULL,
        raw_content LONGTEXT,
        content LONGTEXT,
        summary LONGTEXT,
        metadata LONGTEXT,
        ai_status VARCHAR(32) DEFAULT 'pending',
        ai_error LONGTEXT,
        ai_attempts INT DEFAULT 0,
        crawled_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed BOOLEAN DEFAULT FALSE,
        embedding_status VARCHAR(32),
        UNIQUE KEY uq_crawled_articles_url (url(255))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS article_categories (
        article_id BIGINT NOT NULL,
        category_name VARCHAR(255) NOT NULL,
        PRIMARY KEY (article_id, category_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS podcasts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        title TEXT,
        date VARCHAR(32),
        content_json LONGTEXT,
        audio_generated BOOLEAN DEFAULT FALSE,
        audio_path TEXT,
        banner_img_path TEXT,
        tts_engine VARCHAR(64) DEFAULT 'elevenlabs',
        language_code VARCHAR(32) DEFAULT 'en',
        sources_json LONGTEXT,
        banner_images LONGTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL,
        task_type VARCHAR(64),
        description TEXT,
        command TEXT NOT NULL,
        frequency INT NOT NULL,
        frequency_unit VARCHAR(32) NOT NULL,
        enabled BOOLEAN DEFAULT TRUE,
        last_run TIMESTAMP NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_executions (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        task_id BIGINT NOT NULL,
        start_time TIMESTAMP NOT NULL,
        end_time TIMESTAMP NULL,
        status VARCHAR(32) NOT NULL,
        error_message LONGTEXT,
        output LONGTEXT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS podcast_configs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL,
        description TEXT,
        prompt LONGTEXT NOT NULL,
        time_range_hours INT DEFAULT 24,
        limit_articles INT DEFAULT 20,
        is_active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        tts_engine VARCHAR(64) DEFAULT 'elevenlabs',
        language_code VARCHAR(32) DEFAULT 'en',
        podcast_script_prompt LONGTEXT,
        image_prompt LONGTEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_state (
        session_id VARCHAR(255) PRIMARY KEY,
        state JSON,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        post_id VARCHAR(255) PRIMARY KEY,
        platform VARCHAR(64),
        user_display_name TEXT,
        user_handle VARCHAR(255),
        user_profile_pic_url TEXT,
        post_timestamp VARCHAR(64),
        post_display_time VARCHAR(64),
        post_url TEXT,
        post_text LONGTEXT,
        post_mentions LONGTEXT,
        engagement_reply_count INT,
        engagement_retweet_count INT,
        engagement_like_count INT,
        engagement_bookmark_count INT,
        engagement_view_count INT,
        media LONGTEXT,
        media_count INT,
        is_ad BOOLEAN,
        sentiment VARCHAR(64),
        categories LONGTEXT,
        tags LONGTEXT,
        analysis_reasoning LONGTEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS slack_thread_sessions (
        thread_key VARCHAR(255) PRIMARY KEY,
        session_id VARCHAR(255) NOT NULL,
        channel_id VARCHAR(255) NOT NULL,
        user_id VARCHAR(255),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS slack_session_state (
        session_id VARCHAR(255) PRIMARY KEY,
        state_data LONGTEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS article_chunks (
        id          BIGINT PRIMARY KEY AUTO_INCREMENT,
        article_id  BIGINT NOT NULL,
        chunk_index INT    NOT NULL,
        chunk_text  TEXT   NOT NULL,
        created_at  TEXT NOT NULL,
        INDEX idx_ac_article_id (article_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS podcast_sessions (
        session_id VARCHAR(255) NOT NULL PRIMARY KEY,
        user_id TEXT,
        memory JSON,
        session_data JSON,
        extra_data JSON,
        created_at BIGINT,
        updated_at BIGINT,
        agent_id TEXT,
        team_session_id TEXT,
        agent_data JSON
    )
    """,
]


MYSQL_INDEX_DDLS = [
    "CREATE INDEX idx_source_feeds_source_id ON source_feeds(source_id)",
    "CREATE INDEX idx_sources_is_active ON sources(is_active)",
    "CREATE INDEX idx_source_feeds_is_active ON source_feeds(is_active)",
    "CREATE INDEX idx_source_categories_source_id ON source_categories(source_id)",
    "CREATE INDEX idx_source_categories_category_id ON source_categories(category_id)",
    "CREATE INDEX idx_feed_entries_feed_id ON feed_entries(feed_id)",
    "CREATE INDEX idx_feed_entries_crawl_status ON feed_entries(crawl_status)",
    "CREATE INDEX idx_crawled_articles_ai_status ON crawled_articles(ai_status)",
    "CREATE INDEX idx_crawled_articles_processed ON crawled_articles(processed)",
    "CREATE INDEX idx_article_categories_category_name ON article_categories(category_name)",
    "CREATE INDEX idx_podcasts_date ON podcasts(date)",
    "CREATE INDEX idx_tasks_enabled ON tasks(enabled)",
    "CREATE INDEX idx_tasks_last_run ON tasks(last_run)",
    "CREATE INDEX idx_task_executions_task_id ON task_executions(task_id)",
    "CREATE INDEX idx_task_executions_status ON task_executions(status)",
    "CREATE INDEX idx_session_state_session_id ON session_state(session_id)",
    "CREATE INDEX idx_posts_platform ON posts(platform)",
    "CREATE INDEX idx_posts_user_handle ON posts(user_handle)",
    "CREATE INDEX idx_slack_thread_sessions_session_id ON slack_thread_sessions(session_id)",
]


def _is_duplicate_index_error(error):
    message = str(error).lower()
    return "duplicate key name" in message or "already exists" in message


def init_mysql_schema():
    if not os.environ.get("DATABASE_URL", "").startswith(("mysql://", "mysql+pymysql://")):
        return

    with db_connection("unused_for_mysql") as conn:
        cursor = conn.cursor()
        for ddl in MYSQL_TABLE_DDLS:
            cursor.execute(ddl)

        for ddl in MYSQL_INDEX_DDLS:
            try:
                cursor.execute(ddl)
            except Exception as e:
                if not _is_duplicate_index_error(e):
                    raise

        # Scheduler stores subprocess stdout/stderr; tools often emit Unicode box-drawing etc.
        # If these columns were created under latin1 (older MySQL defaults), INSERT fails with 1366.
        cursor.execute(
            """
            ALTER TABLE task_executions
                MODIFY error_message LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
                MODIFY output LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
            """
        )

        conn.commit()
