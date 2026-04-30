#!/usr/bin/env python3
"""
CLI 端到端自动化测试

设计原则：
  1. 功能完备 — 覆盖所有 CLI 命令
  2. 数据完整 — SELECT 验证入库行数
  3. 数据可用 — 验证下游（因子/回测）能消费上游数据

分层执行（按风险/耗时递增）：
  Tier 1 — 只读命令（db status, factor list, universe, paper status）
  Tier 2 — 计算命令（select, score, factor calc）
  Tier 3 — 数据写入（data bulk-import 轻量子集, data download list）
  Tier 4 — 重型命令（backtest, paper trade/reset）

用法：
    python3 tests/test_cli.py                  # 运行全部
    python3 tests/test_cli.py --tier 1         # 只跑 Tier 1
    python3 tests/test_cli.py --tier 1,2       # 跑 Tier 1+2
    python3 tests/test_cli.py --tier 1,2,3,4   # 跑全部
    python3 tests/test_cli.py --list           # 列出所有测试用例
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# 路径与日志
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI = PROJECT_ROOT / "cli.py"
PYTHON = sys.executable  # 当前 Python 解释器

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_cli")

# ============================================================
# 测试框架
# ============================================================

_results: list[dict] = []


def run_cli(*args, timeout=300, expect_fail=False) -> subprocess.CompletedProcess:
    """执行 CLI 命令，返回 CompletedProcess。"""
    cmd = [PYTHON, str(CLI)] + list(args)
    logger.info("RUN: python3 cli.py %s", " ".join(args))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "core.settings"},
    )
    if not expect_fail and result.returncode != 0:
        logger.error("STDOUT:\n%s", result.stdout[-2000:] if result.stdout else "(empty)")
        logger.error("STDERR:\n%s", result.stderr[-2000:] if result.stderr else "(empty)")
    return result


def db_count(table: str) -> int:
    """直接查数据库行数。"""
    # 延迟导入，避免在 --list 时也初始化 Django
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    import django
    django.setup()
    # DatabaseManager 已废弃
    db = None  # DatabaseManager 已废弃
    df = db.query(f"SELECT COUNT(*) as cnt FROM {table}")
    return int(df.iloc[0]["cnt"])


class TestCase:
    """一个测试用例。"""
    def __init__(self, tier: int, name: str, func, description: str = ""):
        self.tier = tier
        self.name = name
        self.func = func
        self.description = description


# 注册器
_tests: list[TestCase] = []


def test(tier: int, description: str = ""):
    """装饰器：注册测试用例。"""
    def decorator(func):
        _tests.append(TestCase(tier, func.__name__, func, description))
        return func
    return decorator


# ============================================================
# Tier 1 — 只读命令
# ============================================================

@test(1, "CLI --help 正常加载")
def test_help():
    r = run_cli("--help")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "量化交易系统" in r.stdout, "help 文本缺失"


@test(1, "db status 正常返回")
def test_db_status():
    r = run_cli("db", "status")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    # 应该输出表名
    assert "stock_basic" in r.stdout or "daily_price" in r.stdout, "db status 输出缺少表信息"


@test(1, "db init 幂等执行")
def test_db_init():
    r = run_cli("db", "init")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "初始化完成" in r.stdout, "未看到完成消息"


@test(1, "factor list --market us 列出因子")
def test_factor_list_us():
    r = run_cli("factor", "list", "--market", "us")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "EP" in r.stdout, "factor list 缺少 EP"
    assert "MOM_1M" in r.stdout, "factor list 缺少 MOM_1M"


@test(1, "factor list --market cn 列出因子")
def test_factor_list_cn():
    r = run_cli("factor", "list", "--market", "cn")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "MACRO_CYCLE" in r.stdout, "factor list 缺少 MACRO_CYCLE"


@test(1, "universe --market us 返回股票池")
def test_universe_us():
    r = run_cli("universe", "--market", "us", "--date", "2025-01-15", "--limit", "5")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "US 股票池" in r.stdout, "输出缺少股票池标题"


@test(1, "universe --market cn 返回股票池")
def test_universe_cn():
    r = run_cli("universe", "--market", "cn", "--limit", "5")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "CN 股票池" in r.stdout, "输出缺少股票池标题"


@test(1, "paper status --market us")
def test_paper_status_us():
    r = run_cli("paper", "status", "--market", "us")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "US 模拟账户" in r.stdout, "输出缺少账户标题"


@test(1, "paper status --market cn")
def test_paper_status_cn():
    r = run_cli("paper", "status", "--market", "cn")
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "CN 模拟账户" in r.stdout, "输出缺少账户标题"


@test(1, "无效命令返回非零退出码")
def test_invalid_command():
    r = run_cli("nonexistent", expect_fail=True)
    assert r.returncode != 0, "无效命令应返回非零退出码"


@test(1, "data download 无效 target 返回错误")
def test_invalid_target():
    r = run_cli("data", "download", "--target", "foobar", expect_fail=True)
    assert r.returncode != 0, "无效 target 应返回非零退出码"


# ============================================================
# Tier 2 — 计算命令（依赖已有数据）
# ============================================================

@test(2, "select --market us 选股")
def test_select_us():
    r = run_cli("select", "--market", "us", "--date", "2025-01-15", "--top", "5", timeout=600)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    # 可能有结果也可能因数据不足无结果，但不应报错
    assert "运行 US 选股" in r.stdout, "未开始选股"


@test(2, "select --market cn 选股")
def test_select_cn():
    r = run_cli("select", "--market", "cn", "--top", "5", timeout=600)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "运行 CN 选股" in r.stdout, "未开始选股"


@test(2, "score AAPL 查看单股得分")
def test_score_us():
    r = run_cli("score", "AAPL", "--date", "2025-01-15", timeout=600)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "AAPL" in r.stdout, "输出缺少 AAPL"


@test(2, "factor calc EP --market us")
def test_factor_calc_ep():
    r = run_cli("factor", "calc", "EP", "--market", "us", "--date", "2025-01-15", "--top", "5", timeout=600)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "计算 EP" in r.stdout, "未开始因子计算"


@test(2, "factor calc MOM_1M --market us")
def test_factor_calc_mom():
    r = run_cli("factor", "calc", "MOM_1M", "--market", "us", "--date", "2025-01-15", "--top", "5", timeout=600)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "计算 MOM_1M" in r.stdout, "未开始因子计算"


@test(2, "factor calc 无效因子报错")
def test_factor_calc_invalid():
    r = run_cli("factor", "calc", "NONEXISTENT_FACTOR", "--market", "us", expect_fail=True)
    assert r.returncode != 0, "无效因子应返回非零退出码"


# ============================================================
# Tier 3 — 数据写入（轻量子集）
# ============================================================

@test(3, "data bulk-import --source fmp --target stock-list 写入 us_stock_basic")
def test_bulk_import_stock_list():
    r = run_cli("data", "bulk-import", "--source", "fmp", "--target", "stock-list", timeout=120)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "完成" in r.stdout, "未看到完成消息"
    cnt = db_count("us_stock_basic")
    assert cnt > 0, f"us_stock_basic 行数为 {cnt}，期望 > 0"
    logger.info("us_stock_basic rows: %d", cnt)


@test(3, "data bulk-import --source fmp --target sp500 写入 SP500 成分")
def test_bulk_import_sp500():
    r = run_cli("data", "bulk-import", "--source", "fmp", "--target", "sp500", timeout=120)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "完成" in r.stdout, "未看到完成消息"


@test(3, "data bulk-import 幂等性：stock-list 两次不翻倍")
def test_bulk_import_idempotent():
    cnt_before = db_count("us_stock_basic")
    r = run_cli("data", "bulk-import", "--source", "fmp", "--target", "stock-list", timeout=120)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    cnt_after = db_count("us_stock_basic")
    # upsert 幂等：行数不应翻倍（允许小幅增长是因为新股上市）
    ratio = cnt_after / max(cnt_before, 1)
    assert ratio < 1.5, f"行数翻倍了: {cnt_before} → {cnt_after} (ratio={ratio:.2f})"
    logger.info("幂等性检查: %d → %d (ratio=%.2f)", cnt_before, cnt_after, ratio)


@test(3, "data download --market cn --target list 写入 stock_basic")
def test_download_cn_list():
    r = run_cli("data", "download", "--market", "cn", "--target", "list", timeout=120)
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "完成" in r.stdout, "未看到完成消息"
    cnt = db_count("stock_basic")
    assert cnt > 0, f"stock_basic 行数为 {cnt}，期望 > 0"
    logger.info("stock_basic rows: %d", cnt)


@test(3, "data bulk-import 无效 source 报错")
def test_bulk_import_invalid_source():
    r = run_cli("data", "bulk-import", "--source", "invalid_src", "--target", "all", expect_fail=True)
    assert r.returncode != 0, "无效 source 应返回非零退出码"


# ============================================================
# Tier 4 — 重型命令（回测、模拟交易）
# ============================================================

@test(4, "backtest --market us --start 2024-01-01 --end 2024-06-30 (半年)")
def test_backtest_us():
    r = run_cli(
        "backtest", "--market", "us",
        "--start", "2024-01-01", "--end", "2024-06-30",
        timeout=600,
    )
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "回测绩效" in r.stdout or "总耗时" in r.stdout, "未看到回测结果"


@test(4, "backtest --market us --strategy-type beta")
def test_backtest_us_beta():
    r = run_cli(
        "backtest", "--market", "us",
        "--start", "2024-01-01", "--end", "2024-06-30",
        "--strategy-type", "beta",
        timeout=600,
    )
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "总耗时" in r.stdout, "未看到回测结果"


@test(4, "backtest --market us --strategy-type baseline")
def test_backtest_us_baseline():
    r = run_cli(
        "backtest", "--market", "us",
        "--start", "2024-01-01", "--end", "2024-06-30",
        "--strategy-type", "baseline",
        timeout=600,
    )
    assert r.returncode == 0, f"exit code: {r.returncode}"
    assert "总耗时" in r.stdout, "未看到回测结果"


@test(4, "paper reset + trade + status 完整流程 (US)")
def test_paper_full_cycle_us():
    # 1. 重置
    r = run_cli("paper", "reset", "--market", "us")
    assert r.returncode == 0, f"reset exit code: {r.returncode}"
    assert "已重置" in r.stdout, "未看到重置消息"

    # 2. 执行交易
    r = run_cli("paper", "trade", "--market", "us", "--date", "2025-01-15", timeout=600)
    assert r.returncode == 0, f"trade exit code: {r.returncode}"
    # 可能选不出股或交易成功
    assert "交易完成" in r.stdout or "跳过交易" in r.stdout, "未看到交易结果"

    # 3. 检查状态
    r = run_cli("paper", "status", "--market", "us")
    assert r.returncode == 0, f"status exit code: {r.returncode}"
    assert "US 模拟账户" in r.stdout, "输出缺少账户标题"


@test(4, "paper reset + trade + status 完整流程 (CN)")
def test_paper_full_cycle_cn():
    r = run_cli("paper", "reset", "--market", "cn")
    assert r.returncode == 0, f"reset exit code: {r.returncode}"
    assert "已重置" in r.stdout, "未看到重置消息"

    r = run_cli("paper", "trade", "--market", "cn", timeout=600)
    assert r.returncode == 0, f"trade exit code: {r.returncode}"
    assert "交易完成" in r.stdout or "跳过交易" in r.stdout, "未看到交易结果"

    r = run_cli("paper", "status", "--market", "cn")
    assert r.returncode == 0, f"status exit code: {r.returncode}"


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="CLI 端到端测试")
    parser.add_argument("--tier", default="1,2,3,4", help="要运行的 tier，逗号分隔 (默认: 1,2,3,4)")
    parser.add_argument("--list", action="store_true", help="列出所有测试用例")
    parser.add_argument("--stop-on-fail", action="store_true", help="遇到第一个失败即停止")
    args = parser.parse_args()

    if args.list:
        for tc in _tests:
            print(f"  [Tier {tc.tier}] {tc.name}: {tc.description}")
        return

    tiers = {int(t.strip()) for t in args.tier.split(",")}
    selected = [tc for tc in _tests if tc.tier in tiers]

    logger.info("=" * 60)
    logger.info("CLI 端到端测试 — Tier %s — %d 个用例", args.tier, len(selected))
    logger.info("=" * 60)

    passed = 0
    failed = 0
    skipped = 0
    t0 = time.time()

    for tc in selected:
        logger.info("")
        logger.info("--- [Tier %d] %s: %s ---", tc.tier, tc.name, tc.description)
        try:
            tc.func()
            logger.info("  ✅ PASS")
            passed += 1
            _results.append({"tier": tc.tier, "name": tc.name, "status": "PASS"})
        except AssertionError as e:
            logger.error("  ❌ FAIL: %s", e)
            failed += 1
            _results.append({"tier": tc.tier, "name": tc.name, "status": "FAIL", "error": str(e)})
            if args.stop_on_fail:
                logger.error("--stop-on-fail: 停止测试")
                break
        except subprocess.TimeoutExpired:
            logger.error("  ⏰ TIMEOUT")
            failed += 1
            _results.append({"tier": tc.tier, "name": tc.name, "status": "TIMEOUT"})
            if args.stop_on_fail:
                break
        except Exception as e:
            logger.error("  💥 ERROR: %s", e)
            failed += 1
            _results.append({"tier": tc.tier, "name": tc.name, "status": "ERROR", "error": str(e)})
            if args.stop_on_fail:
                break

    elapsed = time.time() - t0

    # 汇总
    logger.info("")
    logger.info("=" * 60)
    logger.info("测试结果汇总")
    logger.info("=" * 60)
    for r in _results:
        status_icon = {"PASS": "✅", "FAIL": "❌", "TIMEOUT": "⏰", "ERROR": "💥"}.get(r["status"], "?")
        err = f" — {r.get('error', '')}" if r.get("error") else ""
        logger.info("  %s [Tier %d] %s%s", status_icon, r["tier"], r["name"], err)
    logger.info("")
    logger.info("  通过: %d  失败: %d  总计: %d  耗时: %.1fs", passed, failed, passed + failed, elapsed)
    logger.info("=" * 60)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
