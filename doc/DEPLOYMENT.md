# 部署指南

## 架构

```
阿里云 ECS (1核2G ¥60/月)          台式机 (i5-13400F/64G)        Mac
─────────────────────          ──────────────────────      ─────────
PostgreSQL                     rsync 拉 parquet             写代码
quant download (cron)          quant backtest               git push
导出 parquet                   quant score (实时选股)
         ↓ Tailscale ↓                ↑
         ───── parquet 文件 ──────────┘
```

ECS 通过 OSS 内网上传 parquet，台式机/Mac 从 OSS 公网下载。

**月费估算：**
- ECS 1核2G: ~¥60
- OSS 存储 19GB: ~¥2.3
- OSS 下载流量 19GB×30天: 首次19GB + 后续增量 ~1GB/天 ≈ ¥5
- **合计: ~¥70/月**

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

### 3. 迁移数据
```bash
# 在能连旧 DB 的机器上导出
pg_dump -h test-solab-or-nginx-pgsql.aws.solab.ai -p 5432 \
  -U newsmore_rw -d qa -n quant -F c -f quant_backup.dump

# 上传到 ECS
scp quant_backup.dump user@ecs-tailscale-ip:~/

# 导入
pg_restore -h localhost -U quant_user -d quantdb \
  -n quant --no-owner --no-privileges quant_backup.dump
```

### 4. 部署 quant binary
```bash
# 在 ECS 上编译
sudo apt install -y build-essential pkg-config libssl-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env

git clone <your-repo> ~/quantization
cd ~/quantization/quant-engine
cargo build --release

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

FMP_API_KEY=你的key
TUSHARE_TOKEN=你的token
FRED_API_KEY=你的key
QUIVER_API_KEY=你的key
EOF
chmod 600 /opt/quant-engine/.env
```

### 6. OSS 配置 (内网中转 parquet)
```bash
# 安装 ossutil
wget https://gosspublic.alicdn.com/ossutil/install.sh && bash install.sh

# 配置 (用内网 endpoint，ECS→OSS 免费)
ossutil config -e oss-cn-shanghai-internal.aliyuncs.com -i <AccessKeyId> -k <AccessKeySecret>

# 创建 bucket (在阿里云控制台创建，选和 ECS 同区域)
# Bucket: 你的bucket
# 区域: 与 ECS 同区 (如 cn-shanghai)
# 存储类型: 标准
# 权限: 私有
```

### 7. Parquet 导出脚本
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

tables = [
    "us_daily_price", "us_financial_data", "us_key_metric",
    "us_index_daily", "us_enterprise_value", "us_analyst_recommendation",
    "us_earnings_surprise", "us_eps_estimate", "us_corporate_action",
    "us_insider_trade", "us_industry_class", "us_shares_float",
    "us_dark_pool_volume", "us_institutional_holder", "us_employee_count",
    "us_congress_trade", "us_gov_contract", "us_lobbying",
    "us_revenue_segment", "us_esg_rating", "us_macro_indicator",
    "us_stock_basic",
    "a_daily_price", "a_financial_income", "a_financial_balance",
    "a_financial_cashflow", "a_financial_indicator", "a_stock_basic",
    "a_index_daily", "a_industry_class", "a_macro_indicator",
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

### 7. Cron 定时任务
```bash
crontab -e
```
```cron
# A 股增量 (每天 16:30 盘后)
30 16 * * 1-5 cd /opt/quant-engine && ./quant download --source tushare --target all --incremental >> /var/log/quant-cn.log 2>&1

# 美股增量 (每天 05:00 北京时间 = 美东 17:00 盘后)
0 5 * * * cd /opt/quant-engine && ./quant download --source fmp --target all --incremental >> /var/log/quant-us.log 2>&1

# FRED 宏观 (每周一 06:00)
0 6 * * 1 cd /opt/quant-engine && ./quant download --source fred >> /var/log/quant-fred.log 2>&1

# 导出 parquet + 上传 OSS (每天 07:00，美股更新完之后)
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
cargo build --release
```

### 2. 同步 Parquet (从 OSS 拉)
```bash
# 安装 ossutil
# Linux: wget https://gosspublic.alicdn.com/ossutil/install.sh && bash install.sh
# Mac: brew install ossutil

# 配置 (用 ECS 同一个 AccessKey)
ossutil config -e oss-cn-shanghai.aliyuncs.com -i <AccessKeyId> -k <AccessKeySecret>

# 手动拉
ossutil sync oss://你的bucket/quant-cache/ ~/quantization/cache/ --delete

# 定时拉 (每天 07:30，ECS 上传完成后)
crontab -e
# 30 7 * * * ossutil sync oss://你的bucket/quant-cache/ ~/quantization/cache/ --delete >> ~/quant-sync.log 2>&1
```

### 3. 跑回测
```bash
cd ~/quantization/quant-engine
./target/release/quant backtest --start 2020-01-01 --end 2025-12-31
```

### 4. 实时选股 (每日自动)
```cron
# A 股选股 (每天 08:30 开盘前，parquet 已同步)
30 8 * * 1-5 cd ~/quantization/quant-engine && ./target/release/quant score --date today --market cn >> ~/quant-score.log 2>&1

# 美股选股 (每天 21:00 盘前)
0 21 * * 1-5 cd ~/quantization/quant-engine && ./target/release/quant score --date today --market us >> ~/quant-score.log 2>&1
```

---

## 四、数据流时间线 (每日)

```
16:30  ECS: A 股增量更新 (Tushare)
05:00  ECS: 美股增量更新 (FMP)
06:00  ECS: FRED 宏观 (周一)
07:00  ECS: 导出 parquet → 上传 OSS (内网，免费)
07:30  台式机: ossutil sync 从 OSS 拉 parquet (增量 ~1GB, 几分钟)
08:30  台式机: A 股选股 → 输出持仓建议
21:00  台式机: 美股选股 → 输出持仓建议
```

---

## 五、安全

1. **PostgreSQL 只监听 localhost**：不开公网端口
2. **ECS 安全组**：只开 SSH 22 端口
3. **OSS Bucket 私有**：AccessKey 访问，不开公网读
4. **.env 权限**：`chmod 600`
5. **API Key 不进 git**：.env 在 .gitignore
