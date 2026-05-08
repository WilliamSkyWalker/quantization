-- ============================================================================
-- 时间戳列治理：类型统一 + 自动维护 created_at / updated_at
-- ----------------------------------------------------------------------------
-- 适用：quant schema 下所有有 created_at / updated_at 的表（约 84 列）
--
-- 操作（每列循环执行）：
--   1. 类型统一：timestamp without time zone → timestamp with time zone
--      转换语义：现有裸值都是 UTC wall clock（sqlx/Django/NOW() 路径），
--      USING col AT TIME ZONE 'UTC' 是无损等价转换
--      已是 timestamptz 的列跳过此步
--   2. 历史 NULL 行回填：北京时间 2026-05-08 09:00:00 = UTC 01:00:00
--   3. ALTER COLUMN SET DEFAULT NOW()
--   4. updated_at 列加 BEFORE UPDATE trigger（每次 UPDATE 自动刷新）
--      created_at 不加 trigger（INSERT 时 DEFAULT 一次即可）
--
-- ⚠️ 性能/锁：ALTER COLUMN TYPE 取 ACCESS EXCLUSIVE，期间该表任何 SELECT/
-- INSERT/UPDATE 都阻塞。整个 migration 在单 transaction 内，预计：
--   us_daily_price (49M) ALTER ~5-15 分钟
--   全部加起来 ~15-30 分钟
-- 跑前确保没有 cron / 用户在用 quant DB。
-- ============================================================================

BEGIN;

-- ── Step 1: 创建通用 trigger 函数 ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION quant.set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── Step 2: updated_at 列循环处理 ──────────────────────────────────────────
DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT table_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'quant' AND column_name = 'updated_at'
        ORDER BY table_name
    LOOP
        -- 2.1 类型统一 (already-tz 跳过)
        IF rec.data_type = 'timestamp without time zone' THEN
            EXECUTE format(
                'ALTER TABLE quant.%I ALTER COLUMN updated_at TYPE timestamp with time zone USING updated_at AT TIME ZONE ''UTC''',
                rec.table_name
            );
            RAISE NOTICE '  TYPE: % updated_at -> timestamptz', rec.table_name;
        END IF;

        -- 2.2 回填 NULL = BJ 2026-05-08 09:00
        EXECUTE format(
            'UPDATE quant.%I SET updated_at = ''2026-05-08 09:00:00+08:00''::timestamptz WHERE updated_at IS NULL',
            rec.table_name
        );

        -- 2.3 DEFAULT NOW()
        EXECUTE format(
            'ALTER TABLE quant.%I ALTER COLUMN updated_at SET DEFAULT NOW()',
            rec.table_name
        );

        -- 2.4 重建 BEFORE UPDATE trigger
        EXECUTE format('DROP TRIGGER IF EXISTS trg_set_updated_at ON quant.%I', rec.table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON quant.%I FOR EACH ROW EXECUTE FUNCTION quant.set_updated_at()',
            rec.table_name
        );

        RAISE NOTICE 'updated_at: % done', rec.table_name;
    END LOOP;
END $$;

-- ── Step 3: created_at 列循环处理（无 trigger） ─────────────────────────────
DO $$
DECLARE
    rec record;
BEGIN
    FOR rec IN
        SELECT table_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'quant' AND column_name = 'created_at'
        ORDER BY table_name
    LOOP
        IF rec.data_type = 'timestamp without time zone' THEN
            EXECUTE format(
                'ALTER TABLE quant.%I ALTER COLUMN created_at TYPE timestamp with time zone USING created_at AT TIME ZONE ''UTC''',
                rec.table_name
            );
            RAISE NOTICE '  TYPE: % created_at -> timestamptz', rec.table_name;
        END IF;

        EXECUTE format(
            'UPDATE quant.%I SET created_at = ''2026-05-08 09:00:00+08:00''::timestamptz WHERE created_at IS NULL',
            rec.table_name
        );

        EXECUTE format(
            'ALTER TABLE quant.%I ALTER COLUMN created_at SET DEFAULT NOW()',
            rec.table_name
        );

        RAISE NOTICE 'created_at: % done', rec.table_name;
    END LOOP;
END $$;

-- ── Step 4: 验证 ───────────────────────────────────────────────────────────
-- 4.1 全部应该是 timestamp with time zone
SELECT data_type, COUNT(*) AS cols
FROM information_schema.columns
WHERE table_schema='quant' AND column_name IN ('created_at','updated_at')
GROUP BY data_type;
-- 期望: 单行 timestamp with time zone, cols ~84

-- 4.2 NULL 计数 (示例几张表)
SELECT 'us_daily_price'     AS tbl, COUNT(*) FILTER (WHERE updated_at IS NULL) AS still_null FROM quant.us_daily_price
UNION ALL SELECT 'us_macro_indicator', COUNT(*) FILTER (WHERE updated_at IS NULL) FROM quant.us_macro_indicator
UNION ALL SELECT 'a_daily_price',      COUNT(*) FILTER (WHERE updated_at IS NULL) FROM quant.a_daily_price;
-- 期望: 全 0

-- 4.3 trigger 数量
SELECT COUNT(*) AS trigger_count FROM information_schema.triggers
WHERE trigger_schema='quant' AND trigger_name='trg_set_updated_at';
-- 期望: 等于 updated_at 列数 (~75)

-- 4.4 DEFAULT 全部就位
SELECT COUNT(*) AS without_default FROM information_schema.columns
WHERE table_schema='quant'
  AND column_name IN ('created_at','updated_at')
  AND column_default IS NULL;
-- 期望: 0

COMMIT;

-- ============================================================================
-- 回滚（如出问题；注意 ALTER COLUMN TYPE 回滚也要 rewrite，同样耗时）
-- ============================================================================
-- BEGIN;
-- DO $$ DECLARE rec record; BEGIN
--   FOR rec IN SELECT table_name FROM information_schema.columns
--              WHERE table_schema='quant' AND column_name='updated_at' LOOP
--     EXECUTE format('DROP TRIGGER IF EXISTS trg_set_updated_at ON quant.%I', rec.table_name);
--     EXECUTE format('ALTER TABLE quant.%I ALTER COLUMN updated_at DROP DEFAULT', rec.table_name);
--     EXECUTE format('ALTER TABLE quant.%I ALTER COLUMN updated_at TYPE timestamp without time zone USING updated_at AT TIME ZONE ''UTC''', rec.table_name);
--   END LOOP;
-- END $$;
-- DROP FUNCTION IF EXISTS quant.set_updated_at();
-- COMMIT;
-- ============================================================================
