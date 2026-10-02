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
echo "$(date -Is) CHECK: Tushare DNS"
# Resolve once per run so a broken WSL DNS relay cannot interrupt a long import.
# Never persist proxy fake-IP answers: they are valid only for the current run.
tushare_ip="$(timeout 5s getent ahostsv4 api.tushare.pro | awk 'NR == 1 {print $1}' || true)"
if [[ -z "$tushare_ip" ]] && command -v dig >/dev/null; then
    for dns_server in 223.5.5.5 1.1.1.1; do
        tushare_ip="$(dig +time=2 +tries=1 +short @"$dns_server" api.tushare.pro A | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print; exit}' || true)"
        if [[ -n "$tushare_ip" ]]; then
            echo "$(date -Is) DNS: system resolver unavailable; using $dns_server"
            break
        fi
    done
fi
if [[ -z "$tushare_ip" ]]; then
    echo "$(date -Is) ERROR: cannot resolve api.tushare.pro; check system DNS or install dig for fallback"
    exit 1
fi
export QUANT_TUSHARE_API_IP="$tushare_ip"
echo "$(date -Is) DNS: api.tushare.pro resolved to $tushare_ip for this run (HTTPS verification enabled)"
echo "$(date -Is) START: A-share incremental update ${extra_args[*]}"
# Bound hung runs so subsequent scheduled updates can proceed.
status=0
timeout --signal=TERM --kill-after=60s 12h \
    ./target/release/quant --market cn download --source tushare --target all --incremental "${extra_args[@]}" \
    || status=$?
echo "$(date -Is) END: downloader exit_code=$status (check downloader logs for endpoint errors)"
exit "$status"
