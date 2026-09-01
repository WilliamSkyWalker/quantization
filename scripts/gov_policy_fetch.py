#!/usr/bin/env python3
"""
部委政策公告原文抓取脚本 (工信部 MIIT + 商务部 MOFCOM)

数据源(均为公开无认证接口, 已实测验证):
1. 工信部 - search-front-server 全文检索接口, cateid=57 覆盖全部文件类型(文件发布/公告/通知/命令等)
   正文已内嵌在检索结果 infoextends.infoContent 字段里, 无需二次请求详情页
2. 商务部 - jpaas-publish-server 分页列表接口(paramJson pageNo), 只返回标题+日期+链接
   正文需要单独请求详情页 art_xxx.html, 用 class="art-con" 提取

用法:
    python3 gov_policy_fetch.py --mode backfill      # 全量翻页回补
    python3 gov_policy_fetch.py --mode incremental   # 只抓最近N页, 遇到已入库的doc_code即停止
"""
import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import pymysql
import requests
from bs4 import BeautifulSoup


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

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
REQUEST_DELAY_SEC = 0.5
INCREMENTAL_MAX_PAGES = 3
MAX_RETRIES = 3

MIIT_SEARCH_URL = "https://www.miit.gov.cn/search-front-server/api/search/info"
MOFCOM_LIST_URL = "https://www.mofcom.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit"
MOFCOM_LIST_PARAMS = dict(
    parseType="bulidstatic",
    webId="8f43c7ad3afc411fb56f281724b73708",
    tplSetId="52551ea0e2c14bca8c84792f7aa37ead",
    pageType="column",
    tagId="分页列表",
    editType="null",
    pageId="fc8bdff48fa345a48b651c1285b70b8f",
)
MOFCOM_PAGE_SIZE = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/william/quantization/logs/gov_policy_fetch.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def existing_doc_codes(conn, source):
    with conn.cursor() as cur:
        cur.execute("SELECT doc_code FROM a_gov_policy_raw WHERE source=%s", (source,))
        return {row[0] for row in cur.fetchall()}


def upsert_rows(conn, rows):
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO a_gov_policy_raw
               (source, doc_code, title, file_number, publish_dept, doc_type, publish_date, content, url, fetched_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                 title=VALUES(title), file_number=VALUES(file_number), publish_dept=VALUES(publish_dept),
                 doc_type=VALUES(doc_type), publish_date=VALUES(publish_date), content=VALUES(content),
                 url=VALUES(url), fetched_at=VALUES(fetched_at)""",
            rows,
        )
    conn.commit()
    return len(rows)


def strip_html(html):
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()


def request_with_retry(url, params=None, method="get"):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.request(method, url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            logger.warning(f"请求失败 attempt={attempt} url={url}: {e}, 重试")
            time.sleep(1.0 * attempt)
    return None


# ---------- MIIT ----------

def parse_miit_item(gd):
    """从一条 groupData.data 记录里解析出结构化字段"""
    url = gd.get("url") or ""
    m = re.search(r"art_([a-f0-9]{32})", url)
    doc_code = m.group(1) if m else (url or gd.get("indexcode", ""))[:64]
    title = gd.get("title") or ""
    file_number = gd.get("filenumbername") or ""
    publish_dept = gd.get("publishgroupname") or gd.get("xxgkextend2") or ""
    doc_type = gd.get("typename") or gd.get("columnname") or ""
    deploytime = gd.get("deploytime")
    publish_date = None
    if deploytime:
        try:
            publish_date = datetime.datetime.fromtimestamp(int(deploytime) / 1000).date()
        except (ValueError, OSError):
            pass

    content_text = ""
    infoextends = gd.get("infoextends")
    if infoextends:
        try:
            ie = json.loads(infoextends)
            info_content = ie.get("infoContent")
            if info_content:
                fields = json.loads(info_content)
                for f in fields:
                    if f.get("fieldName") == "content" and f.get("fieldValue"):
                        content_text = strip_html(f["fieldValue"])
                        break
        except (json.JSONDecodeError, TypeError):
            pass

    full_url = "https://www.miit.gov.cn" + url if url.startswith("/") else url
    return doc_code, title, file_number, publish_dept, doc_type, publish_date, content_text, full_url


def fetch_miit(conn, mode):
    existing = existing_doc_codes(conn, "miit") if mode == "backfill" else set()
    page_size = 30
    select_fields = (
        "title,content,deploytime,_index,url,cdate,infoextends,infocontentattribute,"
        "columnname,filenumbername,publishgroupname,publishtime,metaid,bexxgk,columnid,"
        "xxgkextend1,xxgkextend2,themename,typename,indexcode,createdate"
    )
    sort_fields = json.dumps([{"name": "deploytime", "type": "desc"}], ensure_ascii=False)

    p = 1
    total_new = 0
    max_pages = INCREMENTAL_MAX_PAGES if mode == "incremental" else 10**9
    stop = False
    while not stop and p <= max_pages:
        params = {
            "websiteid": "110000000000000",
            "scope": "basic",
            "q": "",
            "pg": page_size,
            "cateid": 57,
            "pos": "title_text,infocontent,titlepy",
            "begin": "",
            "end": "",
            "dateField": "deploytime",
            "selectFields": select_fields,
            "group": "distinct",
            "level": 6,
            "sortFields": sort_fields,
            "p": p,
        }
        try:
            r = request_with_retry(MIIT_SEARCH_URL, params=params)
            d = r.json()
        except Exception as e:
            logger.warning(f"MIIT p={p} 请求/解析失败: {e}")
            break

        results = d.get("data", {}).get("searchResult", {}).get("dataResults", [])
        total_hits = d.get("data", {}).get("searchResult", {}).get("totalHits", 0)
        if not results:
            logger.info(f"MIIT p={p} 无更多数据，结束(totalHits={total_hits})")
            break

        rows = []
        now = datetime.datetime.now()
        for item in results:
            gd_list = item.get("groupData") or []
            gd = gd_list[0].get("data", {}) if gd_list else item.get("data", {})
            if not gd:
                continue
            doc_code, title, file_number, publish_dept, doc_type, publish_date, content_text, full_url = parse_miit_item(gd)
            if not title or not doc_code:
                continue
            if mode == "incremental" and doc_code in existing:
                stop = True
                continue
            rows.append((
                "miit", doc_code, title[:500], file_number[:200] if file_number else None,
                publish_dept[:300] if publish_dept else None, doc_type[:50] if doc_type else None,
                publish_date, content_text, full_url[:500] if full_url else None, now,
            ))
        n = upsert_rows(conn, rows)
        total_new += n
        logger.info(f"MIIT p={p}/{(total_hits + page_size - 1)//page_size if total_hits else '?'} 新增/更新 {n} 条 (totalHits={total_hits})")
        if p * page_size >= total_hits:
            break
        p += 1
        time.sleep(REQUEST_DELAY_SEC)
    logger.info(f"MIIT 抓取完成，总计 {total_new} 条")
    return total_new


# ---------- MOFCOM ----------

def fetch_mofcom_detail(url):
    r = request_with_retry(url)
    soup = BeautifulSoup(r.content, "html.parser")
    art_con_divs = soup.find_all("div", class_="art-con")
    content_parts = []
    file_number, publish_dept, publish_date_str = None, None, None
    for div in art_con_divs:
        if "art-con-gonggao" in " ".join(div.get("class", [])) or div.find("div", class_="art-con-gonggao"):
            gonggao = div if "art-con-gonggao" in " ".join(div.get("class", [])) else div.find("div", class_="art-con-gonggao")
            text = gonggao.get_text("\n") if gonggao else ""
            m_dept = re.search(r"【发布单位】\s*([^\n]+)", text)
            m_num = re.search(r"【发布文号】\s*([^\n]+)", text)
            m_date = re.search(r"【发文日期】\s*([^\n]+)", text)
            if m_dept:
                publish_dept = m_dept.group(1).strip()
            if m_num:
                file_number = m_num.group(1).strip()
            if m_date:
                publish_date_str = m_date.group(1).strip()
        else:
            content_parts.append(div.get_text("\n").strip())
    content_text = "\n".join(p for p in content_parts if p)
    return content_text, file_number, publish_dept, publish_date_str


def parse_cn_date_loose(s):
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def fetch_mofcom(conn, mode):
    existing = existing_doc_codes(conn, "mofcom") if mode == "backfill" else set()
    page_no = 1
    total_new = 0
    max_pages = INCREMENTAL_MAX_PAGES if mode == "incremental" else 10**9
    stop = False
    while not stop and page_no <= max_pages:
        param_json = json.dumps({"pageNo": page_no, "pageSize": MOFCOM_PAGE_SIZE}, ensure_ascii=False)
        params = dict(MOFCOM_LIST_PARAMS)
        params["paramJson"] = param_json
        try:
            r = request_with_retry(MOFCOM_LIST_URL, params=params)
            d = r.json()
        except Exception as e:
            logger.warning(f"MOFCOM p={page_no} 请求/解析失败: {e}")
            break
        if not d.get("success"):
            logger.info(f"MOFCOM p={page_no} success=false，结束")
            break
        html = d.get("data", {}).get("html", "")
        items = re.findall(
            r'<a href="([^"]*)" title="([^"]*)"[^>]*>[^<]*</a><span>\[([^\]]*)\]</span>', html
        )
        count_m = re.search(r'count="(\d+)"', html)
        total_count = int(count_m.group(1)) if count_m else None
        if not items:
            logger.info(f"MOFCOM p={page_no} 无更多数据，结束")
            break

        now = datetime.datetime.now()
        rows = []
        for url, title, date_str in items:
            m = re.search(r"art_([a-f0-9]{32})", url)
            doc_code = m.group(1) if m else url[:64]
            if mode == "incremental" and doc_code in existing:
                stop = True
                continue
            full_url = url if url.startswith("http") else "https://www.mofcom.gov.cn" + url
            try:
                content_text, file_number, publish_dept, publish_date_str = fetch_mofcom_detail(full_url)
            except Exception as e:
                logger.warning(f"MOFCOM 详情页抓取失败 doc_code={doc_code} url={full_url}: {e}")
                content_text, file_number, publish_dept, publish_date_str = "", None, None, None
            publish_date = parse_cn_date_loose(publish_date_str) or parse_cn_date_loose(date_str)
            rows.append((
                "mofcom", doc_code, title[:500], file_number[:200] if file_number else None,
                publish_dept[:300] if publish_dept else None, None,
                publish_date, content_text, full_url[:500], now,
            ))
            time.sleep(0.2)
        n = upsert_rows(conn, rows)
        total_new += n
        logger.info(f"MOFCOM p={page_no} 新增/更新 {n} 条 (total_count={total_count})")
        if total_count and page_no * MOFCOM_PAGE_SIZE >= total_count:
            break
        page_no += 1
        time.sleep(REQUEST_DELAY_SEC)
    logger.info(f"MOFCOM 抓取完成，总计 {total_new} 条")
    return total_new


def run(mode):
    conn = get_conn()
    fetch_miit(conn, mode)
    fetch_mofcom(conn, mode)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["backfill", "incremental"], required=True)
    args = parser.parse_args()
    run(args.mode)
