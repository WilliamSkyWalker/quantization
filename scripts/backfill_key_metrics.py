#!/usr/bin/env python3
"""
回填 us_key_metric 全量季度数据（key-metrics + ratios）

用法:
    python3 scripts/backfill_key_metrics.py                  # 全部 ticker
    python3 scripts/backfill_key_metrics.py --tickers AAPL MSFT  # 指定 ticker
    python3 scripts/backfill_key_metrics.py --skip-ratios    # 只跑 key-metrics
    python3 scripts/backfill_key_metrics.py --migrate-only   # 只做 DDL 迁移不拉数据

流程:
    1. ALTER TABLE 补齐新增列（幂等，已存在则跳过）
    2. 遍历 ticker 调用 /stable/key-metrics + /stable/ratios
    3. upsert 写入 us_key_metric
"""

import argparse
import logging
import sys
import os

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager, USKeyMetric

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def migrate_key_metric_columns(db: DatabaseManager):
    """ALTER TABLE us_key_metric ADD COLUMN ... — 幂等，已存在则跳过。"""
    from sqlalchemy import Float, String, inspect

    inspector = inspect(db.engine)
    existing_cols = {col["name"] for col in inspector.get_columns("us_key_metric")}

    # 从 ORM model 获取所有列定义
    new_cols = []
    for col in USKeyMetric.__table__.columns:
        if col.name not in existing_cols and col.name != "id":
            new_cols.append(col)

    if not new_cols:
        logger.info("us_key_metric 表结构已是最新，无需迁移")
        return

    from sqlalchemy import text
    with db.engine.begin() as conn:
        for col in new_cols:
            col_type = col.type.compile(db.engine.dialect)
            comment = col.comment or ""
            sql = f"ALTER TABLE us_key_metric ADD COLUMN `{col.name}` {col_type} NULL COMMENT '{comment}'"
            try:
                conn.execute(text(sql))
                logger.info(f"  + {col.name} ({col_type})")
            except Exception as e:
                if "Duplicate column" in str(e):
                    logger.debug(f"  列 {col.name} 已存在，跳过")
                else:
                    raise

    logger.info(f"us_key_metric 新增 {len(new_cols)} 列")


def main():
    parser = argparse.ArgumentParser(description="回填 us_key_metric 全量季度数据")
    parser.add_argument("--tickers", nargs="+", help="指定 ticker（默认全部）")
    parser.add_argument("--skip-ratios", action="store_true", help="跳过 ratios 端点")
    parser.add_argument("--migrate-only", action="store_true", help="只做 DDL 迁移")
    args = parser.parse_args()

    db = DatabaseManager()

    # Step 1: DDL 迁移
    logger.info("=" * 60)
    logger.info("Step 1: 检查/迁移 us_key_metric 表结构")
    logger.info("=" * 60)
    migrate_key_metric_columns(db)

    if args.migrate_only:
        logger.info("--migrate-only 模式，跳过数据回填")
        return

    # Step 2: 回填数据
    from services.data.bulk_downloader import BulkDownloader
    dl = BulkDownloader(db)

    tickers = args.tickers or None

    logger.info("=" * 60)
    logger.info("Step 2: 回填 key-metrics")
    logger.info("=" * 60)
    n_km = dl.download_fmp_key_metrics(tickers=tickers)
    logger.info(f"key-metrics 完成: {n_km} 条")

    if not args.skip_ratios:
        logger.info("=" * 60)
        logger.info("Step 3: 回填 ratios")
        logger.info("=" * 60)
        n_r = dl.download_fmp_ratios(tickers=tickers)
        logger.info(f"ratios 完成: {n_r} 条")

    logger.info("=" * 60)
    logger.info("回填完成!")
    logger.info("=" * 60)

    # Step 3: 验证
    result = db.query("SELECT COUNT(*) as cnt, COUNT(market_cap) as has_mc FROM us_key_metric")
    total = result["cnt"].iloc[0]
    has_mc = result["has_mc"].iloc[0]
    logger.info(f"验证: us_key_metric 总行数={total}, market_cap 非空={has_mc} ({has_mc/max(total,1)*100:.1f}%)")


if __name__ == "__main__":
    main()
