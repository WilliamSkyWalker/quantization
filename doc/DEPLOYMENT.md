# 部署指南

## 架构

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│ 阿里云 ECS      │     │ 台式机 i5/64G    │     │ Mac (开发)  │
│ 2核4G ¥150/月   │     │                  │     │             │
│                 │     │                  │     │             │
│ PostgreSQL      │◄────│ quant backtest   │     │ 写代码      │
│ quant download  │     │ (读 parquet)     │     │ git push    │
│ cron 每日5:00   │────►│ parquet cache    │     │             │
│                 │     │                  │     │             │
└─────────────────┘     └──────────────────┘     └─────────────┘
      数据更新              回测+策略研究              开发
```

---

## 一、阿里云 ECS 配置

### 1. 购买 ECS
- 规格：ecs.c7.large (2核4G) 或 ecs.t6-c2m4.large
- 系统：Ubuntu 22.04 LTS
- 磁盘：100GB ESSD
- 带宽：按量付费 (API 调用流量小)
- 安全组：开放 5432 (PostgreSQL，限 IP) + 22 (SSH)

### 2. 安装 PostgreSQL
```bash
sudo apt update && sudo apt install -y postgresql postgresql-contrib

# 切换到 postgres 用户创建数据库和角色
sudo -u postgres psql <<'SQL'
CREATE USER quant_user WITH PASSWORD '你的密码';
CREATE DATABASE quantdb OWNER quant_user;
\c quantdb
CREATE SCHEMA quant AUTHORIZATION quant_user;
ALTER USER quant_user SET search_path TO quant, public;
SQL

# 允许远程连接（台式机回测需要读 DB）
echo "host all quant_user 0.0.0.0/0 md5" | sudo tee -a /etc/postgresql/14/main/pg_hba.conf
sudo sed -i "s/#listen_addresses = 'localhost'/listen_addresses = '*'/" /etc/postgresql/14/main/postgresql.conf

# 调优（4G 内存）
sudo tee -a /etc/postgresql/14/main/postgresql.conf <<'CONF'
shared_buffers = 1GB
effective_cache_size = 3GB
work_mem = 16MB
maintenance_work_mem = 256MB
max_connections = 50
CONF

sudo systemctl restart postgresql
```

### 3. 迁移数据
```bash
# 在能连旧 DB 的机器上导出
pg_dump -h test-solab-or-nginx-pgsql.aws.solab.ai -p 5432 -U newsmore_rw -d qa -n quant -F c -f quant_backup.dump

# 上传到 ECS
scp quant_backup.dump user@ecs-ip:~/

# 在 ECS 上导入
pg_restore -h localhost -U quant_user -d quantdb -n quant --no-owner --no-privileges quant_backup.dump
```

### 4. 部署 quant binary
```bash
# 在 Mac 上交叉编译 Linux 版本（或在 ECS 上编译）
# 方式 A：ECS 上编译
sudo apt install -y build-essential pkg-config libssl-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
cd /opt
git clone <your-repo> quantization
cd quantization/quant-engine
cargo build --release

# 方式 B：Mac 交叉编译（更快）
# brew install filosottile/musl-cross/musl-cross
# cargo build --release --target x86_64-unknown-linux-musl
# scp target/x86_64-unknown-linux-musl/release/quant user@ecs-ip:/opt/quant-engine/

# 部署文件
mkdir -p /opt/quant-engine
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

### 6. 设置 Cron 定时任务
```bash
# 美股盘后更新（北京时间凌晨 5 点 = 美东下午 5 点）
# A 股盘后更新（北京时间下午 4 点）
crontab -e
```
```cron
# 美股增量更新 (每天 05:00)
0 5 * * * cd /opt/quant-engine && ./quant download --source fmp --target all --incremental >> /var/log/quant-us.log 2>&1

# A 股增量更新 (每天 16:30)
30 16 * * 1-5 cd /opt/quant-engine && ./quant download --source tushare --target all --incremental >> /var/log/quant-cn.log 2>&1

# FRED 宏观 (每周一 06:00)
0 6 * * 1 cd /opt/quant-engine && ./quant download --source fred >> /var/log/quant-fred.log 2>&1

# 日志轮转
0 0 * * 0 find /var/log/quant-*.log -size +100M -exec truncate -s 0 {} \;
```

### 7. 导出 Parquet（供台式机回测）
```bash
# 在 ECS 上装 Python 导出工具（或用 Rust 后续实现）
# 暂时用 Python 一行脚本：
pip install pandas sqlalchemy psycopg2-binary pyarrow

cat > /opt/quant-engine/export_parquet.py <<'PYTHON'
import os, pandas as pd, sqlalchemy as sa

engine = sa.create_engine(f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}@localhost:5432/{os.environ['DB_DATABASE']}")
tables = ["us_daily_price", "us_financial_data", "us_key_metric", "us_index_daily",
          "us_enterprise_value", "us_analyst_recommendation", "us_earnings_surprise",
          "us_eps_estimate", "us_corporate_action", "us_insider_trade",
          "us_industry_class", "us_shares_float", "us_dark_pool_volume",
          "us_institutional_holder", "us_employee_count", "us_congress_trade",
          "us_gov_contract", "us_lobbying", "us_revenue_segment",
          "us_esg_rating", "us_macro_indicator"]

os.makedirs("/opt/quant-engine/cache", exist_ok=True)
for t in tables:
    print(f"Exporting {t}...")
    df = pd.read_sql_table(t, engine, schema="quant")
    df.to_parquet(f"/opt/quant-engine/cache/{t}.parquet", index=False)
    print(f"  {len(df)} rows")
PYTHON
```

```cron
# 每天导出 parquet (在增量更新之后)
0 7 * * * cd /opt/quant-engine && source .env && python3 export_parquet.py >> /var/log/quant-export.log 2>&1
```

---

## 二、台式机 (i5-13400F / 64G) 配置

### 1. 安装 Rust + 编译
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
git clone <your-repo> ~/quantization
cd ~/quantization/quant-engine
cargo build --release
```

### 2. 同步 Parquet Cache
```bash
# 方式 A：rsync 从 ECS 拉取
rsync -avz --progress user@ecs-ip:/opt/quant-engine/cache/ ~/quantization/cache/

# 方式 B：定时拉取（每天 08:00）
crontab -e
# 0 8 * * * rsync -avz user@ecs-ip:/opt/quant-engine/cache/ ~/quantization/cache/ >> /var/log/quant-sync.log 2>&1
```

### 3. 跑回测
```bash
cd ~/quantization/quant-engine
./target/release/quant backtest --start 2020-01-01 --end 2025-12-31 --cache-dir ../cache
```

### 4. .env（连远程 DB，用于 db-status 等查询）
```bash
cat > ~/quantization/.env <<'EOF'
DB_HOST=ecs的公网IP
DB_PORT=5432
DB_USER=quant_user
DB_PASSWORD=你的密码
DB_DATABASE=quantdb
DB_SCHEMA=quant
EOF
```

---

## 三、安全注意事项

1. **PostgreSQL 安全组**：只允许台式机 IP + ECS 本地访问 5432
2. **.env 权限**：`chmod 600 .env`
3. **API Key 不要提交 git**：.env 已在 .gitignore
4. **SSH 用密钥认证**：禁用密码登录
5. **定期备份 DB**：
```cron
# 每周日凌晨备份
0 3 * * 0 pg_dump -h localhost -U quant_user quantdb -F c -f /opt/backups/quant_$(date +\%Y\%m\%d).dump
find /opt/backups -name "*.dump" -mtime +30 -delete
```

---

## 四、日常运维

```bash
# 查看增量更新日志
tail -f /var/log/quant-us.log

# 手动触发美股更新
cd /opt/quant-engine && ./quant download --source fmp --target all --incremental

# 查看数据库状态
cd /opt/quant-engine && ./quant db-status

# 查看磁盘使用
du -sh /var/lib/postgresql/  # DB 数据
du -sh /opt/quant-engine/cache/  # Parquet cache
```
