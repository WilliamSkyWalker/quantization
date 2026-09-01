#!/usr/bin/env python3
"""
宏观新闻/政策利率数据抓取脚本

数据源:
1. 新闻联播文字稿 (AkShare news_cctv, 源自 tv.cctv.com 官方文字稿, 公开无认证, 支持 2016-02-03 至今)
2. 央行存款准备金率历史 (AkShare macro_china_reserve_requirement_ratio)
3. LPR 报价历史 (AkShare macro_china_lpr)

用法:
    python3 macro_news_fetch.py --mode backfill   # 全历史回补(新闻联播逐日 + RRR/LPR全量)
    python3 macro_news_fetch.py --mode incremental  # 增量:只抓最近N天新闻联播 + 刷新RRR/LPR最新记录
"""
import argparse
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path

import akshare as ak
import pymysql


def load_db_config():
    env_path = Path("/home/william/quantization/quant-engine/env.json")
    mysql_cfg = {}
    if env_path.exists():
        with env_path.open() as f:
            env_cfg = json.load(f)
        active_env = env_cfg.get("ENV", "test")
        quant_cfg = env_cfg.get("quant") or {}
        mysql_cfg = quant_cfg.get(active_env) or {}
    return dict(
        host=os.getenv("DB_HOST", mysql_cfg.get("host", "127.0.0.1")),
        port=int(os.getenv("DB_PORT", mysql_cfg.get("port", 3306))),
        user=os.getenv("DB_USER", mysql_cfg.get("user", "root")),
        password=os.getenv("DB_PASSWORD", mysql_cfg.get("password", "")),
        database=os.getenv("DB_NAME", mysql_cfg.get("database", "quant")),
        charset="utf8mb4",
    )


DB_CONFIG = load_db_config()

CCTV_START_DATE = datetime.date(2016, 2, 3)
INCREMENTAL_LOOKBACK_DAYS = 7
REQUEST_DELAY_SEC = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/william/quantization/logs/macro_news_fetch.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def already_fetched_dates(conn, source):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT pub_date FROM a_macro_news_raw WHERE source=%s", (source,))
        return {row[0] for row in cur.fetchall()}


def upsert_cctv_rows(conn, date_str, df):
    if df is None or df.empty:
        return 0
    now = datetime.datetime.now()
    rows = []
    for _, r in df.iterrows():
        title = str(r.get("title", "")).strip()[:500]
        content = str(r.get("content", ""))
        if not title:
            continue
        rows.append((
            "cctv",
            datetime.datetime.strptime(date_str, "%Y%m%d").date(),
            title,
            content,
            now,
        ))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT IGNORE INTO a_macro_news_raw (source, pub_date, title, content, fetched_at)
               VALUES (%s, %s, %s, %s, %s)""",
            rows,
        )
    conn.commit()
    return len(rows)


def fetch_cctv_range(conn, start_date, end_date, mode):
    """逐日抓取新闻联播文字稿"""
    existing = already_fetched_dates(conn, "cctv") if mode == "backfill" else set()
    total_new = 0
    total_days = (end_date - start_date).days + 1
    d = start_date
    day_idx = 0
    while d <= end_date:
        day_idx += 1
        if d in existing:
            d += datetime.timedelta(days=1)
            continue
        date_str = d.strftime("%Y%m%d")
        try:
            df = ak.news_cctv(date=date_str)
            n = upsert_cctv_rows(conn, date_str, df)
            total_new += n
            if n > 0:
                logger.info(f"[{day_idx}/{total_days}] {date_str} 新增 {n} 条")
        except Exception as e:
            logger.warning(f"{date_str} 抓取失败: {e}")
        time.sleep(REQUEST_DELAY_SEC)
        d += datetime.timedelta(days=1)
    logger.info(f"新闻联播抓取完成，总计新增 {total_new} 条")
    return total_new


def upsert_rate_rows(conn, rate_type, rows):
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO a_macro_rate_history
               (rate_type, announce_date, effective_date, value_before, value_after, change_bps, fetched_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                 effective_date=VALUES(effective_date), value_before=VALUES(value_before),
                 value_after=VALUES(value_after), change_bps=VALUES(change_bps), fetched_at=VALUES(fetched_at)""",
            rows,
        )
    conn.commit()
    return len(rows)


def parse_cn_date(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() == "nan":
        return None
    for fmt in ("%Y年%m月%d日", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fetch_rrr(conn):
    df = ak.macro_china_reserve_requirement_ratio()
    rows = []
    now = datetime.datetime.now()
    for _, r in df.iterrows():
        announce = parse_cn_date(r.get("公布时间"))
        if announce is None:
            continue
        effective = parse_cn_date(r.get("生效时间"))
        for rate_type, before_col, after_col in [
            ("RRR_LARGE", "大型金融机构-调整前", "大型金融机构-调整后"),
            ("RRR_SMALL", "中小金融机构-调整前", "中小金融机构-调整后"),
        ]:
            before = r.get(before_col)
            after = r.get(after_col)
            change = None
            try:
                if before is not None and after is not None:
                    change = float(after) - float(before)
            except (TypeError, ValueError):
                pass
            rows.append((rate_type, announce, effective, before, after, change, now))
    n = upsert_rate_rows(conn, "RRR", rows)
    logger.info(f"RRR 历史抓取完成，{n} 条记录写入/更新")
    return n


def fetch_lpr(conn):
    df = ak.macro_china_lpr()
    rows = []
    now = datetime.datetime.now()
    prev_1y, prev_5y = None, None
    for _, r in df.iterrows():
        announce = parse_cn_date(r.get("TRADE_DATE"))
        if announce is None:
            continue
        lpr1y = r.get("LPR1Y")
        lpr5y = r.get("LPR5Y")
        if lpr1y is not None and str(lpr1y).lower() != "nan":
            change = float(lpr1y) - prev_1y if prev_1y is not None else None
            rows.append(("LPR_1Y", announce, announce, prev_1y, float(lpr1y), change, now))
            prev_1y = float(lpr1y)
        if lpr5y is not None and str(lpr5y).lower() != "nan":
            change = float(lpr5y) - prev_5y if prev_5y is not None else None
            rows.append(("LPR_5Y", announce, announce, prev_5y, float(lpr5y), change, now))
            prev_5y = float(lpr5y)
    n = upsert_rate_rows(conn, "LPR", rows)
    logger.info(f"LPR 历史抓取完成，{n} 条记录写入/更新")
    return n


def run(mode):
    conn = get_conn()
    today = datetime.date.today()
    if mode == "backfill":
        fetch_cctv_range(conn, CCTV_START_DATE, today, mode)
    else:
        start = today - datetime.timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
        fetch_cctv_range(conn, start, today, mode)
    fetch_rrr(conn)
    fetch_lpr(conn)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["backfill", "incremental"], required=True)
    args = parser.parse_args()
    run(args.mode)
