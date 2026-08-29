-- One-time migration: preserve the three index_classify fields not present in
-- the original table. Check information_schema.columns before applying it to
-- an existing database.
ALTER TABLE a_industry_class
  ADD COLUMN industry_code VARCHAR(50) NULL AFTER industry_name;
ALTER TABLE a_industry_class
  ADD COLUMN is_pub VARCHAR(20) NULL AFTER industry_code;
ALTER TABLE a_industry_class
  ADD COLUMN parent_code VARCHAR(50) NULL AFTER is_pub;
