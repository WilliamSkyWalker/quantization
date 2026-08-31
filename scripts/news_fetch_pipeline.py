#!/usr/bin/env python3
"""
东方财富新闻搜索接口 -> MySQL 增量抓取管道

数据源: search-api-web.eastmoney.com/search/jsonp (cmsArticleWebOld)
        公开接口，无认证保护，聚合全网主流财经媒体报道 (证券之星/东方财富/蓝鲸财经/中证网等)

已知接口限制 (2026-08-30 实测确认):
  1. 单个关键词查询总条数硬上限 ~1000 条 (pageSize=100 x 最多10页，第11页返回空)
  2. 不支持任何日期区间过滤参数 (beginTime/startTime 等均被忽略)
  3. sort=time 时严格按时间倒序排列且分页边界不重叠 (sort=default 时分页有重叠，不可用于增量判断)
  4. 关键词必须用公司全称 (a_stock_basic.name)，用股票代码搜索会有大量误召回

用法:
  python3 news_fetch_pipeline.py --mode backfill      # 首次运行，每只股票最多翻10页(约1000条上限)补历史
  python3 news_fetch_pipeline.py --mode incremental   # 定时任务用，只抓自上次抓取以来的新文章

数据落表:
  a_news_raw          - 新闻原文 (ts_code, article_code) 联合唯一键，INSERT IGNORE 去重
  a_news_fetch_state  - 每只股票的抓取断点 (last_article_code / last_publish_time)，用于增量判断
"""
import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pymysql
import requests


def load_db_config():
    env_path = Path("/home/william/quantization/quant-engine/env.json")
    mysql_cfg = {}
    if env_path.exists():
        with env_path.open() as f:
            env_cfg = json.load(f)
        mysql_cfg = env_cfg.get("mysql") or env_cfg.get("database") or {}
    return dict(
        host=os.getenv("DB_HOST", mysql_cfg.get("host", "127.0.0.1")),
        port=int(os.getenv("DB_PORT", mysql_cfg.get("port", 3306))),
        user=os.getenv("DB_USER", mysql_cfg.get("user", "root")),
        password=os.getenv("DB_PASSWORD", mysql_cfg.get("password", "")),
        database=os.getenv("DB_NAME", mysql_cfg.get("database", "quant")),
        charset="utf8mb4",
        autocommit=True,
    )


DB_CONFIG = load_db_config()

SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"

REQUEST_DELAY_SEC = 0.35          # 单请求间隔，避免对公开接口造成过大压力
MAX_WORKERS = 8                   # 并发线程数，保守设置
PAGE_SIZE = 100
BACKFILL_MAX_PAGES = 10           # 首次抓取每票最多翻页数 (对应约1000条硬上限)
INCREMENTAL_SAFETY_MAX_PAGES = 3  # 增量抓取每票最多翻页数 (防止单票新闻爆发导致无限翻页)
REQUEST_TIMEOUT_SEC = 10
MAX_RETRY = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/william/quantization/logs/news_fetch_pipeline.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def fetch_news_page(keyword: str, page_index: int, page_size: int = PAGE_SIZE, sort: str = "time"):
    inner_param = {
        "uid": "", "keyword": keyword, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": sort,
            "pageIndex": page_index, "pageSize": page_size,
            "preTag": "<em>", "postTag": "</em>",
        }},
    }
    params = {"cb": "jQuery_pipeline", "param": json.dumps(inner_param, ensure_ascii=False),
              "_": str(int(time.time() * 1000))}
    headers = {"user-agent": "Mozilla/5.0"}

    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = requests.get(SEARCH_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SEC)
            r.raise_for_status()
            txt = r.text
            txt = txt[txt.index("(") + 1: txt.rindex(")")]
            data = json.loads(txt)
            return data.get("result", {}).get("cmsArticleWebOld", []) or []
        except Exception as e:
            last_err = e
            logger.warning("请求失败 keyword=%s page=%d attempt=%d/%d err=%s",
                           keyword, page_index, attempt, MAX_RETRY, e)
            time.sleep(1.0 * attempt)
    raise RuntimeError(f"fetch_news_page 重试{MAX_RETRY}次仍失败: keyword={keyword} page={page_index}") from last_err


def strip_em_tags(s: str) -> str:
    return (s or "").replace("<em>", "").replace("</em>", "")


def resolve_search_keyword(name: str) -> str:
    """
    深市历史遗留的A/B双重股份命名(如 万科A/万科B、深振业A)，用带后缀全称搜索经常0命中，
    去掉末尾单字母A/B后命中率明显更高（实测 2026-08-31：万科A用"万科A"搜索0条，用"万科"搜索3条+）。
    仅对以单个A/B结尾且去掉后长度>=2的名称做该 fallback 尝试，其余名称不受影响。
    """
    if len(name) >= 3 and name[-1] in ("A", "B") and not name[-2].isascii():
        return name[:-1]
    return name


def fetch_ticker_news(ts_code: str, name: str, mode: str, state: dict | None):
    """
    抓取单只股票的新闻。
    返回 (new_rows, newest_article_code, newest_publish_time, status, error_msg)
    """
    last_article_code = state.get("last_article_code") if state else None
    last_publish_time = state.get("last_publish_time") if state else None

    max_pages = BACKFILL_MAX_PAGES if mode == "backfill" else INCREMENTAL_SAFETY_MAX_PAGES
    all_new_rows = []
    newest_code, newest_time = None, None
    hit_cap = False

    search_name = name
    probe_rows = None
    try:
        probe_rows = fetch_news_page(name, 1)
        time.sleep(REQUEST_DELAY_SEC)
    except Exception as e:
        return all_new_rows, newest_code, newest_time, "error", str(e)

    if not probe_rows:
        fallback_name = resolve_search_keyword(name)
        if fallback_name != name:
            logger.info("ts_code=%s name=%s 原名0命中，回退用%s重试", ts_code, name, fallback_name)
            search_name = fallback_name

    for page in range(1, max_pages + 1):
        try:
            rows = probe_rows if (page == 1 and search_name == name and probe_rows is not None) else fetch_news_page(search_name, page)
        except Exception as e:
            return all_new_rows, newest_code, newest_time, "error", str(e)

        time.sleep(REQUEST_DELAY_SEC)
        if not rows:
            break

        if page == 1:
            newest_code = rows[0]["code"]
            newest_time = rows[0]["date"]

        stop = False
        for row in rows:
            article_code = row["code"]
            publish_time = row["date"]
            # 增量模式：遇到已经记录过的最新文章即停止翻页（sort=time 严格倒序，之后的都已抓过）
            if mode == "incremental" and last_article_code and article_code == last_article_code:
                stop = True
                break
            if mode == "incremental" and last_publish_time and publish_time <= str(last_publish_time):
                stop = True
                break
            all_new_rows.append({
                "ts_code": ts_code, "stock_name": name, "article_code": article_code,
                "title": strip_em_tags(row.get("title")),
                "content_summary": strip_em_tags(row.get("content")),
                "media_name": row.get("mediaName"), "url": row.get("url"),
                "publish_time": publish_time,
            })
        if stop:
            break
        if page == max_pages:
            hit_cap = True

    status = "ok_capped" if hit_cap else "ok"
    if hit_cap:
        logger.warning("ts_code=%s name=%s 触发翻页上限(%d页)，可能存在遗漏新闻", ts_code, name, max_pages)
    return all_new_rows, newest_code, newest_time, status, None


def load_universe(conn):
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT ts_code, name FROM a_stock_basic WHERE list_status='L' AND name IS NOT NULL")
        return cur.fetchall()


def load_state_map(conn):
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT ts_code, last_article_code, last_publish_time FROM a_news_fetch_state")
        return {r["ts_code"]: r for r in cur.fetchall()}


def upsert_news_rows(conn, rows):
    if not rows:
        return 0
    sql = """
        INSERT IGNORE INTO a_news_raw
        (ts_code, stock_name, article_code, title, content_summary, media_name, url, publish_time)
        VALUES (%(ts_code)s, %(stock_name)s, %(article_code)s, %(title)s, %(content_summary)s,
                %(media_name)s, %(url)s, %(publish_time)s)
    """
    with conn.cursor() as cur:
        affected = cur.executemany(sql, rows)
    return affected or 0


def upsert_state(conn, ts_code, newest_code, newest_time, status, new_count, error_msg):
    sql = """
        INSERT INTO a_news_fetch_state
        (ts_code, last_article_code, last_publish_time, last_fetch_at, last_run_new_count, last_run_status, last_run_error)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            last_article_code = COALESCE(VALUES(last_article_code), last_article_code),
            last_publish_time = COALESCE(VALUES(last_publish_time), last_publish_time),
            last_fetch_at = VALUES(last_fetch_at),
            last_run_new_count = VALUES(last_run_new_count),
            last_run_status = VALUES(last_run_status),
            last_run_error = VALUES(last_run_error)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ts_code, newest_code, newest_time, datetime.now(), new_count, status,
                           (error_msg or "")[:2000]))


def process_one(ticker, mode, state):
    ts_code, name = ticker["ts_code"], ticker["name"]
    rows, newest_code, newest_time, status, err = fetch_ticker_news(ts_code, name, mode, state)
    return ts_code, name, rows, newest_code, newest_time, status, err


def run(mode: str):
    conn = pymysql.connect(**DB_CONFIG)
    universe = load_universe(conn)
    state_map = load_state_map(conn) if mode == "incremental" else {}
    logger.info("开始抓取 mode=%s universe=%d 只股票", mode, len(universe))

    total_new, total_error, total_capped = 0, 0, 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_one, t, mode, state_map.get(t["ts_code"])): t for t in universe}
        done_count = 0
        for fut in as_completed(futures):
            ts_code, name, rows, newest_code, newest_time, status, err = fut.result()
            done_count += 1
            if status == "error":
                total_error += 1
                logger.error("ts_code=%s name=%s 抓取失败: %s", ts_code, name, err)
                upsert_state(conn, ts_code, None, None, "error", 0, err)
                continue
            if status == "ok_capped":
                total_capped += 1

            inserted = upsert_news_rows(conn, rows)
            total_new += inserted
            upsert_state(conn, ts_code, newest_code, newest_time, status, inserted, None)

            if inserted > 0:
                logger.info("ts_code=%s name=%s 新增%d条", ts_code, name, inserted)
            if done_count % 500 == 0:
                logger.info("进度 %d/%d，已用时 %.1fs", done_count, len(universe), time.time() - t0)

    conn.close()
    elapsed = time.time() - t0
    logger.info("抓取完成 mode=%s 总耗时=%.1fs 新增文章=%d 触发翻页上限票数=%d 失败票数=%d",
                mode, elapsed, total_new, total_capped, total_error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["backfill", "incremental"], required=True)
    args = parser.parse_args()
    run(args.mode)
