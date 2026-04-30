# 部署指南（Rust 版，2026-04-30 更新）

## 架构

```
阿里云 ECS (1核2G ¥60/月)         台式机 (i5-13400F/64G)        Mac
─────────────────────         ──────────────────────       ─────────
PostgreSQL                    rsync 拉 parquet              写代码
quant download (cron)         quant backtest                git push
quant export (parquet 导出)   quant analyze
        ↓ Tailscale ↓               ↑
        ───── parquet 文件 ──────────┘
```

ECS 通过 OSS 内网上传 parquet，台式机/Mac 从 OSS 公网下载。

**月费估算：**
- ECS 1核2G: ~¥60
- OSS 存储 19GB: ~¥2.3
- OSS 下载流量 19GB×30 天: 首次 19GB + 后续增量 ~1GB/天 ≈ ¥5
- FMP Ultimate 数据：~$300/月（不含本地部署成本）
- FRED：免费
- **运维合计: ~¥70/月** + 数据订阅

---

## 一、阿里云 ECS

### 1. 购买
- 规格：ecs.t6-c1m2.large (1核2G)
- 系统：Ubuntu 22.04 LTS
- 磁盘：50GB ESSD
- 带宽：按量付费
- 安全组：只开 22 (SSH) + Tailscale 自动穿透

### 2. 安装 PostgreSQL
```bash
sudo apt update && sudo apt install -y postgresql postgresql-contrib

sudo -u postgres psql <<'SQL'
CREATE USER quant_user WITH PASSWORD '你的密码';
CREATE DATABASE quantdb OWNER quant_user;
\c quantdb
CREATE SCHEMA quant AUTHORIZATION quant_user;
ALTER USER quant_user SET search_path TO quant, public;
SQL

# PostgreSQL 调优 (2G 内存)
sudo tee -a /etc/postgresql/14/main/postgresql.conf <<'CONF'
shared_buffers = 512MB
effective_cache_size = 1536MB
work_mem = 8MB
maintenance_work_mem = 128MB
max_connections = 30
CONF

sudo systemctl restart postgresql
```

### 3. 迁移数据（如有旧 DB）
```bash
# 在能连旧 DB 的机器上导出
pg_dump -h <旧主机> -p 5432 -U <旧用户> -d <旧库> -n quant -F c -f quant_backup.dump

# 上传到 ECS
scp quant_backup.dump user@ecs-tailscale-ip:~/

# 导入
pg_restore -h localhost -U quant_user -d quantdb \
  -n quant --no-owner --no-privileges quant_backup.dump
```

### 4. 部署 Rust quant binary
```bash
# 安装 Rust
sudo apt install -y build-essential pkg-config libssl-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

git clone <your-repo> ~/quantization
cd ~/quantization/quant-engine
cargo build --release -p quant-cli      # 输出 target/release/quant

mkdir -p /opt/quant-engine/cache
cp target/release/quant /opt/quant-engine/
cp config.toml /opt/quant-engine/
```

### 5. 配置 .env
```bash
cat > /opt/quant-engine/.env <<'EOF'
DB_HOST=localhost
DB_PORT=5432
DB_USER=quant_user
DB_PASSWORD=你的密码
DB_DATABASE=quantdb
DB_SCHEMA=quant

FMP_API_KEY=你的key             # FMP Ultimate plan
FRED_API_KEY=你的key             # FRED 免费
TUSHARE_TOKEN=你的token          # A 股
EOF
chmod 600 /opt/quant-engine/.env
```

> **不再需要的环境变量**（v25 已废弃数据源）：QUIVER_API_KEY / UW_API_KEY / FISCAL_API_KEY / ALPHAVANTAGE_API_KEY

### 6. OSS 配置（内网中转 parquet）
```bash
wget https://gosspublic.alicdn.com/ossutil/install.sh && bash install.sh

# 配置 (用内网 endpoint，ECS→OSS 免费)
ossutil config -e oss-cn-shanghai-internal.aliyuncs.com -i <AccessKeyId> -k <AccessKeySecret>

# 在阿里云控制台创建 bucket，与 ECS 同区域，权限私有
```

### 7. Parquet 导出脚本

> **2026-04-30 注**：当前 parquet 导出仍依赖 Python `pandas/pyarrow`（小工具）。
> Rust 端 `quant export-parquet` 命令尚未实现，是 P3 待办之一。
> 临时方案：保留这个 Python 导出脚本但**只用作数据导出工具**，不参与策略逻辑。

```bash
sudo apt install -y python3-pip
pip3 install pandas sqlalchemy psycopg2-binary pyarrow

cat > /opt/quant-engine/export_parquet.sh <<'BASH'
#!/bin/bash
source /opt/quant-engine/.env
cd /opt/quant-engine

python3 - <<'PYTHON'
import os, pandas as pd, sqlalchemy as sa

engine = sa.create_engine(
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@localhost:5432/{os.environ['DB_DATABASE']}",
    connect_args={"options": "-csearch_path=quant,public"}
)

# 仅 v25 production 实际需要的表
tables = [
    # 美股
    "us_daily_price", "us_financial_data", "us_key_metric",
    "us_index_daily", "us_enterprise_value", "us_analyst_recommendation",
    "us_earnings_surprise", "us_eps_estimate", "us_corporate_action",
    "us_insider_trade", "us_industry_class", "us_shares_float",
    "us_employee_count", "us_revenue_segment", "us_esg_rating",
    "us_macro_indicator", "us_stock_basic",
    # A 股
    "a_daily_price", "a_financial_income", "a_financial_balance",
    "a_financial_cashflow", "a_financial_indicator", "a_stock_basic",
    "a_index_daily", "a_industry_class", "a_macro_indicator", "a_trade_cal",
]

os.makedirs("cache", exist_ok=True)
for t in tables:
    try:
        df = pd.read_sql_table(t, engine, schema="quant")
        df.to_parquet(f"cache/{t}.parquet", index=False)
        print(f"{t}: {len(df)} rows")
    except Exception as e:
        print(f"{t}: FAILED - {e}")
PYTHON
BASH
chmod +x /opt/quant-engine/export_parquet.sh
```

### 8. Cron 定时任务
```bash
crontab -e
```
```cron
# A 股增量 (每天 16:30 盘后)
30 16 * * 1-5 cd /opt/quant-engine && ./quant --market cn download --source tushare --target all --incremental >> /var/log/quant-cn.log 2>&1

# 美股增量 (每天 05:00 北京时间 = 美东 17:00 盘后)
0 5 * * * cd /opt/quant-engine && ./quant download --source fmp --target all --incremental >> /var/log/quant-us.log 2>&1

# FRED 宏观 (每周一 06:00)
0 6 * * 1 cd /opt/quant-engine && ./quant download --source fred --target all >> /var/log/quant-fred.log 2>&1

# 导出 parquet + 上传 OSS (每天 07:00)
0 7 * * * /opt/quant-engine/export_parquet.sh && ossutil sync /opt/quant-engine/cache/ oss://你的bucket/quant-cache/ --delete >> /var/log/quant-export.log 2>&1

# 日志清理 (每周日)
0 0 * * 0 find /var/log/quant-*.log -size +100M -exec truncate -s 0 {} \;

# DB 备份 (每周日 03:00)
0 3 * * 0 pg_dump -h localhost -U quant_user quantdb -F c -f /opt/backups/quant_$(date +\%Y\%m\%d).dump && find /opt/backups -mtime +30 -delete
```

---

## 二、台式机 (i5-13400F / 64G)

### 1. 安装 Rust + 编译
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
git clone <your-repo> ~/quantization
cd ~/quantization/quant-engine
cargo build --release -p quant-cli
```

### 2. 同步 Parquet（从 OSS 拉）
```bash
# Linux: wget https://gosspublic.alicdn.com/ossutil/install.sh && bash install.sh
# Mac:   brew install ossutil

ossutil config -e oss-cn-shanghai.aliyuncs.com -i <AccessKeyId> -k <AccessKeySecret>

# 手动拉
ossutil sync oss://你的bucket/quant-cache/ ~/quantization/cache/ --delete

# 定时拉 (每天 07:30，ECS 上传完成后)
crontab -e
# 30 7 * * * ossutil sync oss://你的bucket/quant-cache/ ~/quantization/cache/ --delete >> ~/quant-sync.log 2>&1
```

### 3. 下载 Fama-French 5 因子（一次性）
```bash
cd /tmp && curl -fsSL -o ff5.zip \
  "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
unzip -o ff5.zip
awk 'NR==4 {print "date,Mkt-RF,SMB,HML,RMW,CMA,RF"; next} NR>4 && /^[0-9]{8},/ {print}' \
  F-F_Research_Data_5_Factors_2x3_daily.csv > ~/quantization/cache/ff5_daily.csv
```

### 4. 跑回测
```bash
cd ~/quantization/quant-engine
./target/release/quant backtest --start 2012-01-01 --end 2025-12-31 \
  --cache-dir ../cache --output ../output/rust_v25
# v25 baseline: α=13.28% (t=3.40) / Sharpe 0.99 / Down Capture -8.62%
```

### 5. 因子分析（IC + Fama-MacBeth）
```bash
./target/release/quant analyze --start 2012-01-01 --end 2025-12-31 \
  --cache-dir ../cache --output ../output/factor_analysis
# 输出: output/factor_analysis/ic_summary_*.csv + fama_macbeth_*.csv
```

### 6. 实时选股（每日自动，待 Rust score 命令实装）
```cron
# 待实现 — 当前 backtest 输出 signals 已经够用，score 命令是 backtest 的实时变体
# 目前可用 backtest 起点等于今天的方式间接做实时选股
```

---

## 三、数据流时间线（每日）

```
16:30  ECS: A 股增量 (Tushare)
05:00  ECS: 美股增量 (FMP)
06:00  ECS: FRED 宏观 (周一)
07:00  ECS: 导出 parquet → 上传 OSS (内网，免费)
07:30  台式机: ossutil sync 从 OSS 拉 parquet (增量 ~1GB, 几分钟)
08:30  台式机: A 股策略运行
21:00  台式机: 美股策略运行
```

---

## 四、模拟盘 / 实盘部署（待 Rust trading 完整化）

| 市场 | 当前状态 | 说明 |
|------|---------|------|
| A 股 PaperBroker | ✅ Rust 已实现 (`quant-trading::PaperBroker`) | `quant --market cn trade --account default --signals X.json` |
| A 股 掘金实盘 | ⏳ 待开发 | 已记 P3 待办 |
| 美股 paper | ⏳ Rust 待开发 | 推荐先 Alpaca REST (1-2 天)，后 IBKR TWS。已记 P3 待办 |
| 美股实盘 | ⏳ 待 paper 验证后做 | Alpaca live 或 IBKR live |

---

## 五、安全

1. **PostgreSQL 只监听 localhost**：不开公网端口
2. **ECS 安全组**：只开 SSH 22 端口
3. **OSS Bucket 私有**：AccessKey 访问，不开公网读
4. **.env 权限**：`chmod 600`
5. **API Key 不进 git**：`.env` 在 `.gitignore`
6. **PostgreSQL 慢 SQL 阈值**：sqlx 配 2s（pool.rs，避免 daily upsert 刷屏）
