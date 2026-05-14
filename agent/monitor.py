"""Quick DB monitor — run with: ./venv/bin/python monitor.py"""

import time
import os
import pymysql
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()
raw = os.environ["DATABASE_URL"].replace("mysql+pymysql://", "mysql://")
u = urlparse(raw)
conn = pymysql.connect(
    host=u.hostname,
    port=u.port or 3306,
    user=u.username,
    password=u.password,
    database=u.path.lstrip("/"),
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=True,
)


def q(sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


print("Monitoring... Ctrl+C to stop\n")
while True:
    fe = {r["status"]: r["cnt"] for r in q("SELECT crawl_status status, COUNT(*) cnt FROM feed_entries GROUP BY crawl_status")}
    ca = {r["status"]: r["cnt"] for r in q("SELECT ai_status status, COUNT(*) cnt FROM crawled_articles GROUP BY ai_status")}
    latest = q("SELECT id, LEFT(title,40) title, ai_status, ai_attempts FROM crawled_articles ORDER BY id DESC LIMIT 5")

    os.system("clear")
    print(f"{'=' * 60}")
    print("  feed_entries (URL Processor 消耗 pending)")
    for k, v in sorted(fe.items()):
        print(f"    {k:<12} {v}")
    print("\n  crawled_articles (AI Analyzer 消耗 pending)")
    for k, v in sorted(ca.items()):
        print(f"    {k:<12} {v}")
    print("\n  最新入库的文章 (crawled_articles)")
    for r in latest:
        print(f"    [{r['id']}] {r['title']:<42} {r['ai_status']} attempts={r['ai_attempts']}")
    print(f"{'=' * 60}")
    print(f"  刷新间隔 3s，{time.strftime('%H:%M:%S')}")
    time.sleep(3)
