"""
报告生成模块

自动生成月度策略报告（HTML 格式），包含：
    1. 净值曲线和回撤图
    2. 绩效指标摘要
    3. 持仓明细
    4. 行业分布
    5. 月度收益率热力图
    6. 因子 IC 监控
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import base64
from io import BytesIO

from services.config import LOG_LEVEL, PROJECT_ROOT
from services.monitor.performance import PerformanceAnalyzer

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

plt.rcParams["font.sans-serif"] = ["SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _fig_to_base64(fig) -> str:
    """将 matplotlib 图表转为 base64 编码的 PNG。"""
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    buf.seek(0)
    img_base64 = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return f"data:image/png;base64,{img_base64}"


class ReportGenerator:
    """
    HTML 报告生成器。

    用法:
        gen = ReportGenerator()
        html = gen.generate(
            nav=nav_series,
            benchmark_nav=bm_series,
            holdings=holdings_df,
            factor_ic=ic_dict,
        )
        gen.save(html, "report_2024_12.html")
    """

    def __init__(self, title: str = "A股多因子策略月度报告"):
        self.title = title

    def generate(
        self,
        nav: pd.Series,
        benchmark_nav: Optional[pd.Series] = None,
        holdings: Optional[pd.DataFrame] = None,
        industry_attribution: Optional[pd.DataFrame] = None,
        factor_ic: Optional[dict[str, pd.DataFrame]] = None,
        report_date: Optional[str] = None,
    ) -> str:
        """
        生成完整 HTML 报告。

        Args:
            nav: 策略净值。
            benchmark_nav: 基准净值。
            holdings: 当前持仓 DataFrame[ts_code, weight]。
            industry_attribution: 行业归因 DataFrame。
            factor_ic: {因子名: IC序列DataFrame} 字典。
            report_date: 报告日期（默认今天）。

        Returns:
            完整 HTML 字符串。
        """
        if report_date is None:
            report_date = datetime.now().strftime("%Y-%m-%d")

        sections = []

        # 1. 绩效概要
        analyzer = PerformanceAnalyzer.__new__(PerformanceAnalyzer)
        analyzer.rf_rate = 0.02
        analyzer.rf_daily = (1.02) ** (1 / 252) - 1
        metrics = analyzer.calc_metrics(nav, benchmark_nav)
        sections.append(self._section_metrics(metrics))

        # 2. 净值曲线
        sections.append(self._section_nav_chart(nav, benchmark_nav))

        # 3. 月度收益率
        monthly = PerformanceAnalyzer.monthly_returns(nav)
        sections.append(self._section_monthly_returns(monthly))

        # 4. 持仓明细
        if holdings is not None and not holdings.empty:
            sections.append(self._section_holdings(holdings))

        # 5. 行业归因
        if industry_attribution is not None and not industry_attribution.empty:
            sections.append(self._section_industry(industry_attribution))

        # 6. 因子 IC 监控
        if factor_ic:
            sections.append(self._section_factor_ic(factor_ic))

        html = self._build_html(sections, report_date)
        return html

    # ----------------------------------------------------------
    # 各节内容生成
    # ----------------------------------------------------------

    def _section_metrics(self, metrics: dict) -> str:
        """绩效指标卡片。"""
        cards = ""
        key_metrics = [
            ("年化收益率", metrics.get("年化收益率", 0), True),
            ("最大回撤", metrics.get("最大回撤", 0), False),
            ("夏普比率", metrics.get("夏普比率", 0), True),
            ("Calmar比率", metrics.get("Calmar比率", 0), True),
            ("超额年化收益率", metrics.get("超额年化收益率", None), True),
            ("信息比率", metrics.get("信息比率", None), True),
        ]

        for name, value, higher_better in key_metrics:
            if value is None:
                continue
            if isinstance(value, float):
                if abs(value) < 1:
                    display = f"{value:.2%}"
                else:
                    display = f"{value:.2f}"
                color = "#27ae60" if (value > 0) == higher_better else "#e74c3c"
            else:
                display = str(value)
                color = "#333"

            cards += f"""
            <div class="metric-card">
                <div class="metric-value" style="color:{color}">{display}</div>
                <div class="metric-label">{name}</div>
            </div>"""

        return f'<h2>绩效概要</h2><div class="metrics-grid">{cards}</div>'

    def _section_nav_chart(
        self, nav: pd.Series, benchmark: Optional[pd.Series]
    ) -> str:
        """净值曲线和回撤图。"""
        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(12, 6), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
        )

        ax1.plot(nav.index, nav, label="Strategy", color="#e74c3c", linewidth=1.2)
        if benchmark is not None and not benchmark.empty:
            common = nav.index.intersection(benchmark.index)
            if len(common) > 0:
                bm = benchmark.loc[common] / benchmark.loc[common].iloc[0]
                ax1.plot(common, bm, label="Benchmark", color="#3498db",
                         linewidth=1.2, alpha=0.7)
        ax1.set_ylabel("NAV")
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        cummax = nav.cummax()
        dd = (nav - cummax) / cummax
        ax2.fill_between(dd.index, 0, dd, color="#e74c3c", alpha=0.3)
        ax2.set_ylabel("Drawdown")
        ax2.grid(True, alpha=0.3)
        plt.tight_layout()

        img = _fig_to_base64(fig)
        return f'<h2>净值曲线</h2><img src="{img}" style="width:100%">'

    def _section_monthly_returns(self, monthly: pd.DataFrame) -> str:
        """月度收益率表。"""
        if monthly.empty:
            return ""

        def color_val(val):
            if pd.isna(val):
                return ""
            color = "#27ae60" if val > 0 else "#e74c3c" if val < 0 else "#333"
            return f'style="color:{color};font-weight:bold"'

        rows = ""
        for year, row in monthly.iterrows():
            cells = f"<td><b>{year}</b></td>"
            for val in row:
                if pd.isna(val):
                    cells += "<td>-</td>"
                else:
                    cells += f'<td {color_val(val)}>{val:.1%}</td>'
            rows += f"<tr>{cells}</tr>"

        headers = "<th>年份</th>" + "".join(f"<th>{c}</th>" for c in monthly.columns)
        table = f'<table class="data-table"><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>'

        return f"<h2>月度收益率</h2>{table}"

    def _section_holdings(self, holdings: pd.DataFrame) -> str:
        """持仓明细。"""
        rows = ""
        for _, row in holdings.head(30).iterrows():
            cells = "".join(f"<td>{row.get(c, '')}</td>" for c in holdings.columns)
            rows += f"<tr>{cells}</tr>"

        headers = "".join(f"<th>{c}</th>" for c in holdings.columns)
        table = f'<table class="data-table"><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table>'

        return f"<h2>持仓明细（前30）</h2>{table}"

    def _section_industry(self, attribution: pd.DataFrame) -> str:
        """行业归因。"""
        fig, ax = plt.subplots(figsize=(12, 5))
        colors = ["#27ae60" if v >= 0 else "#e74c3c" for v in attribution["contribution"]]
        ax.barh(attribution["industry_name"], attribution["contribution"], color=colors)
        ax.set_xlabel("Contribution")
        ax.set_title("Industry Attribution")
        ax.grid(True, alpha=0.3, axis="x")
        plt.tight_layout()

        img = _fig_to_base64(fig)
        return f'<h2>行业归因</h2><img src="{img}" style="width:100%">'

    def _section_factor_ic(self, factor_ic: dict) -> str:
        """因子 IC 监控。"""
        rows = ""
        for name, ic_df in factor_ic.items():
            if ic_df.empty:
                continue
            ic = ic_df["ic"]
            ic_mean = ic.mean()
            ic_std = ic.std()
            icir = ic_mean / ic_std if ic_std > 0 else 0
            pos_rate = (ic > 0).mean()

            color = "#27ae60" if abs(ic_mean) > 0.03 else "#f39c12" if abs(ic_mean) > 0.02 else "#e74c3c"

            rows += f"""
            <tr>
                <td>{name}</td>
                <td style="color:{color}">{ic_mean:.4f}</td>
                <td>{ic_std:.4f}</td>
                <td>{icir:.2f}</td>
                <td>{pos_rate:.1%}</td>
                <td>{len(ic)}</td>
            </tr>"""

        table = f"""
        <table class="data-table">
            <thead>
                <tr><th>因子</th><th>IC均值</th><th>IC标准差</th>
                    <th>ICIR</th><th>IC正率</th><th>期数</th></tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>"""

        return f"<h2>因子 IC 监控</h2>{table}"

    # ----------------------------------------------------------
    # HTML 模板
    # ----------------------------------------------------------

    def _build_html(self, sections: list[str], report_date: str) -> str:
        """组装完整 HTML。"""
        body = "\n".join(sections)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{self.title}</title>
<style>
    body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
           max-width: 1100px; margin: 0 auto; padding: 20px;
           background: #f8f9fa; color: #333; }}
    h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
    h2 {{ color: #2c3e50; margin-top: 30px; border-left: 4px solid #3498db;
          padding-left: 12px; }}
    .header {{ text-align: center; margin-bottom: 30px; }}
    .header .date {{ color: #7f8c8d; font-size: 14px; }}
    .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                     gap: 15px; margin: 20px 0; }}
    .metric-card {{ background: white; border-radius: 8px; padding: 20px;
                    text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
    .metric-value {{ font-size: 24px; font-weight: bold; }}
    .metric-label {{ font-size: 12px; color: #7f8c8d; margin-top: 5px; }}
    .data-table {{ width: 100%; border-collapse: collapse; background: white;
                   border-radius: 8px; overflow: hidden;
                   box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
    .data-table th {{ background: #2c3e50; color: white; padding: 10px 12px;
                      text-align: left; font-size: 13px; }}
    .data-table td {{ padding: 8px 12px; border-bottom: 1px solid #ecf0f1;
                      font-size: 13px; }}
    .data-table tr:hover {{ background: #f5f6fa; }}
    img {{ border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
    .footer {{ text-align: center; color: #95a5a6; font-size: 12px;
               margin-top: 40px; padding-top: 20px;
               border-top: 1px solid #ecf0f1; }}
</style>
</head>
<body>
<div class="header">
    <h1>{self.title}</h1>
    <div class="date">报告日期: {report_date}</div>
</div>
{body}
<div class="footer">
    Generated by Quant System | {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
</div>
</body>
</html>"""

    # ----------------------------------------------------------
    # 保存报告
    # ----------------------------------------------------------

    @staticmethod
    def save(html: str, filename: Optional[str] = None) -> str:
        """
        保存 HTML 报告到文件。

        Args:
            html: HTML 内容。
            filename: 文件名（可选，默认按日期命名）。

        Returns:
            保存路径。
        """
        output_dir = PROJECT_ROOT / "output" / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

        filepath = output_dir / filename
        filepath.write_text(html, encoding="utf-8")
        logger.info(f"报告已保存: {filepath}")
        return str(filepath)
