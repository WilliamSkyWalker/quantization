"""
行业因子权重种子数据

预设 4 套因子权重模板：
    - __DEFAULT__: 当前等权行为（向后兼容）
    - 有色金属: 商品周期，重动量
    - 基础化工: 均衡偏质量
    - 计算机: 成长+动量+小盘

weight 列存最终带符号值（反向因子如 TURN_20D 在默认配置中存负值）。
"""

# 全部因子名称
ALL_FACTORS = [
    "EP", "BP",
    "MOM_1M", "MOM_3M", "MOM_12M",
    "ROE_TTM", "GROSS_MARGIN",
    "TURN_20D",
    "VOL_20D", "PRICE_DEV_60D", "REV_5D",
    "PROFIT_STB", "MARGIN_TREND",
    "SIZE", "IND_MOM",
    # Phase 8 新增
    "NET_PROFIT_YOY", "REVENUE_YOY",
    "RESIDUAL_MOM", "VOL_PRICE_DIV",
]

# ============================================================
# 行业权重模板
# ============================================================

# __DEFAULT__: 与现有代码硬编码等权行为一致
_DEFAULT = {
    "EP": 1.0,
    "BP": 1.0,
    "MOM_1M": 1.0,
    "MOM_3M": 1.0,
    "MOM_12M": 1.0,
    "ROE_TTM": 1.0,
    "GROSS_MARGIN": 1.0,
    "TURN_20D": -1.0,          # 反向因子
    "VOL_20D": -0.5,           # 反向因子，防守
    "PRICE_DEV_60D": -0.3,     # 反向因子，防守
    "REV_5D": 0.4,
    "PROFIT_STB": -0.5,        # 反向因子
    "MARGIN_TREND": 0.4,
    "SIZE": 0.3,
    "IND_MOM": 0.5,
    "NET_PROFIT_YOY": 0.8,
    "REVENUE_YOY": 0.6,
    "RESIDUAL_MOM": 0.7,
    "VOL_PRICE_DIV": -0.4,        # 反向因子
}

# 有色金属：商品周期，重动量
_YOUSE_JINSHU = {
    "EP": 0.3,
    "BP": 0.3,
    "MOM_1M": 2.0,
    "MOM_3M": 2.0,
    "MOM_12M": 1.5,
    "ROE_TTM": 0.5,
    "GROSS_MARGIN": 0.5,
    "TURN_20D": -0.5,
    "VOL_20D": 0.0,            # 商品周期波动大，不惩罚
    "PRICE_DEV_60D": 0.0,      # 趋势行情偏离大是正常的
    "REV_5D": 0.3,
    "PROFIT_STB": -0.3,
    "MARGIN_TREND": 0.3,
    "SIZE": 0.3,
    "IND_MOM": 1.5,            # 行业动量加大
    "NET_PROFIT_YOY": 0.5,     # 周期股利润波动大，降低权重
    "REVENUE_YOY": 0.5,
    "RESIDUAL_MOM": 1.0,       # 残差动量在周期股中有效
    "VOL_PRICE_DIV": -0.3,
}

# 基础化工：均衡偏质量
_JICHU_HUAGONG = {
    "EP": 1.0,
    "BP": 1.0,
    "MOM_1M": 1.0,
    "MOM_3M": 1.0,
    "MOM_12M": 1.0,
    "ROE_TTM": 1.5,
    "GROSS_MARGIN": 1.5,
    "TURN_20D": -1.0,
    "VOL_20D": -0.5,
    "PRICE_DEV_60D": -0.3,
    "REV_5D": 0.4,
    "PROFIT_STB": -0.5,
    "MARGIN_TREND": 1.5,       # 毛利率趋势加大
    "SIZE": 0.3,
    "IND_MOM": 0.5,
    "NET_PROFIT_YOY": 1.0,
    "REVENUE_YOY": 0.8,
    "RESIDUAL_MOM": 0.7,
    "VOL_PRICE_DIV": -0.4,
}

# 计算机：成长+动量+小盘
_JISUANJI = {
    "EP": 0.5,
    "BP": 0.3,
    "MOM_1M": 1.5,
    "MOM_3M": 1.5,
    "MOM_12M": 1.0,
    "ROE_TTM": 1.0,
    "GROSS_MARGIN": 1.0,
    "TURN_20D": -0.5,
    "VOL_20D": -0.3,
    "PRICE_DEV_60D": -0.3,
    "REV_5D": 0.5,
    "PROFIT_STB": -0.3,
    "MARGIN_TREND": 1.0,
    "SIZE": -0.5,              # 偏小盘（SIZE 原为正=大盘，取负=偏小盘）
    "IND_MOM": 1.0,
    "NET_PROFIT_YOY": 1.5,    # 成长股重视利润增速
    "REVENUE_YOY": 1.2,       # 成长股重视营收增速
    "RESIDUAL_MOM": 0.8,
    "VOL_PRICE_DIV": -0.3,
}

# 行业→权重模板的映射
_TEMPLATES = {
    "__DEFAULT__": (_DEFAULT, "默认配置（等权基线）"),
    "有色金属": (_YOUSE_JINSHU, "商品周期，重动量"),
    "基础化工": (_JICHU_HUAGONG, "均衡偏质量"),
    "计算机": (_JISUANJI, "成长+动量+小盘"),
}


def get_seed_records() -> list[dict]:
    """
    生成全部种子记录。

    Returns:
        list[dict]，每条包含 industry_name, factor_name, weight, description。
    """
    records = []
    for industry_name, (weights, desc) in _TEMPLATES.items():
        for factor_name, weight in weights.items():
            records.append({
                "industry_name": industry_name,
                "factor_name": factor_name,
                "weight": weight,
                "description": desc,
            })
    return records
