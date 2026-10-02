# A 股数据定时更新

2026-09-26 配置于本机 william 用户的 crontab，系统时区为 Asia/Shanghai。

- 每天 18:30 更新，次日 07:00 补抓；周末也执行，以覆盖公告和延迟发布的数据。
- 入口：`scripts/update_a_share.sh`；任务模板：`scripts/a_share_update.cron`。
- 调用现有 Rust CLI：`quant --market cn download --source tushare --target all --incremental`。
- 覆盖股票列表、交易日历、行业、行情、四类财务报表、指数、宏观、商品、龙虎榜、融资融券、沪深港通资金流及预告、快报、股东交易、回购、解禁等下载器已支持的数据。
- CLI 从 `quant-engine/env.json` 读取数据库及 Tushare 配置；不依赖交互式 shell 环境变量。
- 下载前运行 `quant db-ping`，使用与下载命令相同的配置连接数据库并执行 `SELECT 1`，最多等待 5 秒。连接失败或超时则记录 `SKIP`、以退出码 0 跳过本轮，等待下一次定时触发；不会发起数据抓取或启动数据库。此预检适用于脚本入口，直接执行 `quant download` 不经过预检。
- 日志：`logs/a_share_update_YYYY-MM-DD.log`。互斥锁阻止重叠执行，12 小时超时后终止，60 秒后仍未退出则强制结束。
- 启动时先用系统 DNS 解析 Tushare（最多 5 秒），失败则通过 `dig` 依次查询 `223.5.5.5`、`1.1.1.1`。解析结果仅通过本轮的 `QUANT_TUSHARE_API_IP` 传给下载器，不写 hosts、不持久化代理 fake-IP，也不关闭 HTTPS 证书校验。直接运行 CLI 时不自动启用此脚本的备用解析。DNS 全部失败则明确报错退出。
- Tushare API、数据库读写或工作任务失败会中止更新并返回非零退出码；成功退出仍不等于历史数据完整。
- 此任务复用现有下载器的增量规则，不承诺补齐全部历史缺口。新闻、政策仍由原有三个独立任务负责。
- 本机或 WSL 关闭期间 cron 不执行；恢复后的下一次调度继续按下载器增量规则更新。

手动执行：`/bin/bash scripts/update_a_share.sh`。查看任务：`crontab -l`。
安装模板时应合并现有 crontab，不能直接覆盖其他任务。

## 2026-09-26 写入修复

- 交易日期按日期类型比较，限制到北京时间当天；按日期升序处理，重抓最新一天，避免中途失败后越过缺口。
- 每份 API 数据的所有 SQL 批次放在同一事务中；失败整体回滚。下载器内串行写入，事务使用 READ COMMITTED，锁等待/死锁最多尝试三次；不修改 MySQL 全局配置。
- API 或 SQL 失败后不再标记完成，并停止后续更新。单股票试跑不写全市场日期完成记录。
- 旧程序可能已在最大日期之前留下部分写入，普通增量不会自动发现这些历史缺口。可为 Tushare 按交易日下载的表指定 `--replay-from YYYY-MM-DD` 重抓：

```bash
cd /home/william/quantization/quant-engine
./target/release/quant --market cn download --source tushare --target daily_price --incremental --replay-from 2026-08-28
```

`--replay-from` 作用于行情、龙虎榜、融资融券和沪深港通资金流等按交易日下载的表；财报等接口沿用自身更新逻辑。运行 `--target all` 时也可带此参数。

## 2026-09-26 接口限流与重复代码修复

- Tushare 请求速率上限设为 150 次/分钟（更低的用户配置仍生效），不再让配置的 500 次/分钟超过实测接口上限。HTTP 重试也经过速率限制。
- 遇到 API 40203 时，两个请求队列共享 65 秒冷却；重试前检查任务是否已失败。
- 股票任务按 DISTINCT ts_code 构建。实库 a_stock_basic 原有 27,765 行、5,555 个代码；完整备份到 a_stock_basic_backup_20260926 后保留每个代码最新一行，并新增 uq_a_stock_basic_ts_code 唯一索引。
- 财务及事件表按成功抓取时间记录检查点，12 小时内重启跳过已成功抓取的股票；失败不标记完成。已过期的检查点会重新抓取，以跟进新增或修订公告。
