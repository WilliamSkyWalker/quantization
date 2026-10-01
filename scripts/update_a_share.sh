#!/bin/bash
# Run the existing A-share downloader from cron with a stable working directory.
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
export TZ=Asia/Shanghai

extra_args=()
if (( $# )); then
    if [[ $# -ne 2 || $1 != --replay-from || ! $2 =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        echo "Usage: $0 [--replay-from YYYY-MM-DD]" >&2
        exit 2
    fi
    extra_args=(--replay-from "$2")
fi

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$PROJECT_ROOT/logs"
exec >> "$PROJECT_ROOT/logs/a_share_update_$(date +%F).log" 2>&1

exec 9> "$PROJECT_ROOT/logs/a_share_update.lock"
if ! flock -n 9; then
    echo "$(date -Is) SKIP: A-share update is already running"
    exit 0
fi

cd "$PROJECT_ROOT/quant-engine"
if [[ ! -x ./target/release/quant ]]; then
    echo "$(date -Is) ERROR: quant executable is missing"
    exit 1
fi
echo "$(date -Is) CHECK: database connectivity"
if ! timeout --signal=TERM --kill-after=1s 10s ./target/release/quant db-ping; then
    echo "$(date -Is) SKIP: database unavailable; retry at next scheduled run"
    exit 0
fi
echo "$(date -Is) START: A-share incremental update ${extra_args[*]}"
# Bound hung runs so subsequent scheduled updates can proceed.
status=0
timeout --signal=TERM --kill-after=60s 12h \
    ./target/release/quant --market cn download --source tushare --target all --incremental "${extra_args[@]}" \
    || status=$?
echo "$(date -Is) END: downloader exit_code=$status (check downloader logs for endpoint errors)"
exit "$status"
