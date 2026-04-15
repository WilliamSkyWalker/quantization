"""
批量写入工具 — 纯 Django ORM 实现。

策略：事务内先查询已有行，分流为 bulk_create（新行）+ bulk_update（旧行）。
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from django.utils import timezone
from operator import attrgetter

import pandas as pd
from django.db.models import Q

logger = logging.getLogger(__name__)

_FLOAT_MAX = 1e308


class UpsertManager:
    """异步批量写入，50 线程池，batch 2000。"""

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=50)
        self._futures = []

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    def upsert(self, model_class, records: list[dict], unique_keys: list[str]):
        """
        批量写入：事务内查询已有行 → 新行 bulk_create / 旧行 bulk_update。

        Args:
            model_class: Django Model 类（managed=False）
            records: 待写入的 dict 列表
            unique_keys: 冲突检测列名列表
        """
        if not records:
            return

        meta = model_class._meta
        valid_cols = {f.column for f in meta.get_fields() if hasattr(f, "column")} - {"id"}
        update_fields = sorted(valid_cols - set(unique_keys) - {"id"})

        # 数据清理
        cleaned = []
        for rec in records:
            clean = {}
            for k, v in rec.items():
                if k not in valid_cols:
                    continue
                if isinstance(v, bool):
                    v = int(v)
                elif isinstance(v, float) and (math.isnan(v) or math.isinf(v) or abs(v) > _FLOAT_MAX):
                    v = None
                elif isinstance(v, pd.Timestamp):
                    v = v.to_pydatetime() if pd.notna(v) else None
                clean[k] = v
            if "updated_at" in valid_cols:
                clean["updated_at"] = timezone.now()
            cleaned.append(clean)

        if not cleaned:
            return

        # 异步批量写入
        batch_size = 2000
        for i in range(0, len(cleaned), batch_size):
            batch = cleaned[i:i + batch_size]

            def _do_write(b=batch, mc=model_class, uk=unique_keys, uf=update_fields):
                self._write_batch(mc, b, uk, uf)

            fut = self._pool.submit(_do_write)
            self._futures.append(fut)

        # 背压
        self._futures = [f for f in self._futures if not f.done()]
        if len(self._futures) > 1000:
            done, pending = wait(self._futures, return_when=FIRST_COMPLETED)
            self._futures = list(pending)

        logger.info(f"{meta.db_table}: upsert {len(cleaned)} 条")

    @staticmethod
    def _write_batch(model_class, batch: list[dict], unique_keys: list[str], update_fields: list[str]):
        """单批次写入：查询已有行 → 分流 create/update。"""
        from django.db import connection

        try:
            # 1. 查询已存在的行
            q = Q()
            for row in batch:
                row_q = Q(**{k: row[k] for k in unique_keys if k in row})
                q |= row_q

            existing_map = {}
            for obj in model_class.objects.filter(q):
                key = tuple(getattr(obj, k) for k in unique_keys)
                existing_map[key] = obj

            # 2. 分流
            to_create = []
            to_update = []
            for row in batch:
                key = tuple(row.get(k) for k in unique_keys)
                existing_obj = existing_map.get(key)
                if existing_obj is None:
                    to_create.append(model_class(**row))
                else:
                    for field, val in row.items():
                        setattr(existing_obj, field, val)
                    to_update.append(existing_obj)

            # 3. 写入
            if to_create:
                model_class.objects.bulk_create(to_create, batch_size=2000)
            if to_update:
                model_class.objects.bulk_update(to_update, update_fields, batch_size=2000)
        finally:
            connection.close()

    def upsert_df(self, model_class, df: "pd.DataFrame", unique_keys: list[str]):
        """DataFrame 版 upsert。"""
        if df is None or df.empty:
            logger.debug(f"{model_class._meta.db_table}: DataFrame 为空，跳过")
            return
        records = df.to_dict("records")
        self.upsert(model_class, records, unique_keys)

    def flush(self):
        """等待所有异步写入完成。"""
        if self._futures:
            wait(self._futures)
            for f in self._futures:
                exc = f.exception()
                if exc:
                    logger.warning(f"异步写入失败: {exc}")
            self._futures.clear()

    # ----------------------------------------------------------
    # 便捷方法
    # ----------------------------------------------------------

    def mark_import_done(self, table_name: str, ticker: str):
        """标记导入完成（import_progress 表）。"""
        from data.models import ImportProgress
        ImportProgress.objects.update_or_create(
            table_name=table_name,
            ticker=ticker,
            defaults={"completed_at": timezone.now()},
        )

    def get_import_done_tickers(self, table_name: str) -> set[str]:
        """获取已完成导入的 ticker 集合。"""
        from data.models import ImportProgress
        return set(
            ImportProgress.objects.filter(table_name=table_name)
            .values_list("ticker", flat=True)
        )


# 模块级单例
_manager = None


def get_upsert_manager() -> UpsertManager:
    global _manager
    if _manager is None:
        _manager = UpsertManager()
    return _manager
