-- Required for stock-list upserts to update instead of inserting duplicates.
-- On an existing database, back up and deduplicate ts_code before applying.
-- This ALTER deliberately fails if duplicates remain; it never deletes rows.
-- Applied 2026-09-26 after preserving a_stock_basic_backup_20260926.
ALTER TABLE a_stock_basic
    ADD UNIQUE KEY uq_a_stock_basic_ts_code (ts_code);
