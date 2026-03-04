#!/usr/bin/env bash
#
# A股量化系统 —— 一键启动 / 重启脚本（Ubuntu）
#
# 功能：
#   1. 杀死已有的前后端进程
#   2. 启动后端（daphne ASGI）和前端（vite dev）
#   3. 安装 crontab 定时任务
#
# 用法：
#   chmod +x start.sh
#   ./start.sh              # 完整启动（杀进程 + 启动 + 装 cron）
#   ./start.sh --stop       # 仅停止
#   ./start.sh --cron-only  # 仅安装/更新 crontab
#
set -euo pipefail

# ============================================================
# 路径配置（按实际部署修改）
# ============================================================
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

# Python / Node 可执行文件（如果用 venv 请改为 venv 路径）
PYTHON="${PYTHON:-python3}"
PNPM="${PNPM:-pnpm}"

# 端口
BACKEND_PORT=8000
FRONTEND_PORT=5173

# ============================================================
# 辅助函数
# ============================================================
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

kill_by_port() {
    local port=$1
    local pids
    pids=$(lsof -ti :"$port" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        log "杀死占用端口 $port 的进程: $pids"
        echo "$pids" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi
}

wait_for_port() {
    local port=$1
    local name=$2
    local max_wait=30
    local i=0
    while ! ss -tlnp 2>/dev/null | grep -q ":${port} " && \
          ! lsof -ti :"$port" >/dev/null 2>&1; do
        i=$((i + 1))
        if [[ $i -ge $max_wait ]]; then
            log "WARNING: $name 未能在 ${max_wait}s 内启动，请检查日志"
            return 1
        fi
        sleep 1
    done
    log "$name 已就绪 (port $port)"
}

# ============================================================
# 停止服务
# ============================================================
stop_services() {
    log "=== 停止已有服务 ==="
    kill_by_port $BACKEND_PORT
    kill_by_port $FRONTEND_PORT
    # 也杀掉残留的 daphne / vite 进程
    pkill -f "daphne.*core.asgi" 2>/dev/null || true
    pkill -f "vite.*--port.*$FRONTEND_PORT" 2>/dev/null || true
    log "服务已停止"
}

# ============================================================
# 启动后端
# ============================================================
start_backend() {
    log "=== 启动后端 (daphne :$BACKEND_PORT) ==="
    cd "$BACKEND_DIR"
    cd "$PROJECT_DIR"
    nohup "$PYTHON" -m daphne \
        -b 0.0.0.0 \
        -p "$BACKEND_PORT" \
        backend.core.asgi:application \
        >> "$LOG_DIR/backend.log" 2>&1 &
    log "后端 PID: $!"
    wait_for_port $BACKEND_PORT "后端"
}

# ============================================================
# 启动前端
# ============================================================
start_frontend() {
    log "=== 启动前端 (vite :$FRONTEND_PORT) ==="
    cd "$FRONTEND_DIR"
    nohup "$PNPM" dev --host 0.0.0.0 \
        >> "$LOG_DIR/frontend.log" 2>&1 &
    log "前端 PID: $!"
    wait_for_port $FRONTEND_PORT "前端"
}

# ============================================================
# 安装 Crontab
# ============================================================
install_cron() {
    log "=== 安装定时任务 ==="

    local CRON_TAG="# quant-system-cron"

    # 先移除旧的 quant cron 条目
    crontab -l 2>/dev/null | grep -v "$CRON_TAG" > /tmp/_quant_cron_tmp || true

    cat >> /tmp/_quant_cron_tmp <<EOF

# ====== A股量化系统定时任务（via backend API） ======

# 周一至周五 18:00 — 增量更新全部数据 + 舆情抓取分析（收盘后）
0 18 * * 1-5 curl -sS -X POST http://localhost:$BACKEND_PORT/api/data/update >> $LOG_DIR/cron_update.log 2>&1 $CRON_TAG

# 周一至周五 09:20 — 执行 T+1 交易信号（开盘前）
20 9 * * 1-5 curl -sS -X POST http://localhost:$BACKEND_PORT/api/paper/trade >> $LOG_DIR/cron_trade.log 2>&1 $CRON_TAG

# 每周六 02:00 — 补充利润表数据
0 2 * * 6 curl -sS -X POST http://localhost:$BACKEND_PORT/api/data/backfill-income >> $LOG_DIR/cron_backfill.log 2>&1 $CRON_TAG

# 每小时整点 — 财经媒体快讯抓取（东方财富/财联社/新浪，无历史数据需高频积累）
0 * * * * curl -sS -X POST http://localhost:$BACKEND_PORT/api/sentiment/download -H 'Content-Type: application/json' -d '{"tier":6}' >> $LOG_DIR/cron_news.log 2>&1 $CRON_TAG

# 每月1日 03:00 — 生成上月报告
0 3 1 * * curl -sS -X POST http://localhost:$BACKEND_PORT/api/report/generate -H 'Content-Type: application/json' -d "{\"start_date\":\"\$(date -d '1 month ago' '+\%Y-\%m-01')\",\"end_date\":\"\$(date -d 'yesterday' '+\%Y-\%m-\%d')\"}" >> $LOG_DIR/cron_report.log 2>&1 $CRON_TAG

# ====== END A股量化系统 ======
EOF

    crontab /tmp/_quant_cron_tmp
    rm -f /tmp/_quant_cron_tmp

    log "定时任务已安装，当前 crontab:"
    crontab -l | grep "$CRON_TAG" | sed "s/$CRON_TAG//"
}

# ============================================================
# 主流程
# ============================================================
case "${1:-}" in
    --stop)
        stop_services
        ;;
    --cron-only)
        install_cron
        ;;
    *)
        stop_services
        start_backend
        start_frontend
        install_cron

        # 自动启动 Polymarket 监控
        log "=== 启动 Polymarket 监控 ==="
        curl -sS -X POST "http://localhost:$BACKEND_PORT/api/polymarket/monitor/start" \
            >> "$LOG_DIR/polymarket_monitor.log" 2>&1 \
            && log "Polymarket 监控已启动" \
            || log "WARNING: Polymarket 监控启动失败，请手动启动"

        log "=== 全部就绪 ==="
        log "前端: http://0.0.0.0:$FRONTEND_PORT"
        log "后端: http://0.0.0.0:$BACKEND_PORT/api/"
        log "日志: $LOG_DIR/"
        ;;
esac
