"""System configuration API views — Django ORM 版。"""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from stocks.models import AIndustryClass, AIndustryFactorConfig

logger = logging.getLogger(__name__)

# 不含 token/password 的公开配置项
SAFE_SETTINGS = [
    'MAX_HOLDINGS', 'MIN_HOLDINGS', 'MIN_SELECT_SCORE',
    'MAX_SINGLE_WEIGHT', 'MAX_INDUSTRY_WEIGHT',
    'BUY_COMMISSION', 'SELL_COMMISSION', 'STAMP_TAX', 'SLIPPAGE',
    'MAX_DRAWDOWN_THRESHOLD', 'DRAWDOWN_REDUCE_POSITION',
    'MIN_DAILY_TURNOVER', 'IPO_FILTER_DAYS',
    'NEUTRALIZE_MODE', 'NONLINEAR_SIZE',
    'USE_VOL_TARGETING', 'TARGET_VOL', 'VOL_LOOKBACK_DAYS',
    'VOL_SCALE_MIN', 'VOL_SCALE_MAX',
    'PAPER_INITIAL_CAPITAL', 'PAPER_ACCOUNT_NAME', 'TRADER_TYPE',
    'DATA_START_DATE', 'EXCLUDE_STAR_MARKET',
    'LOG_LEVEL',
    'ALLOWED_INDUSTRIES',
]

SENSITIVE_SETTINGS = [
    'TUSHARE_TOKEN', 'TWITTER_USERNAME', 'TWITTER_EMAIL', 'TWITTER_PASSWORD',
    'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE',
]


@api_view(['GET'])
def get_settings(request):
    """获取当前系统配置（敏感字段只显示是否已配置）。"""
    from services import config as qs

    result = {}
    for key in SAFE_SETTINGS:
        val = getattr(qs, key, None)
        result[key] = val

    sensitive = {}
    for key in SENSITIVE_SETTINGS:
        val = getattr(qs, key, None)
        sensitive[key] = '***已配置***' if val else ''
    result['_sensitive'] = sensitive

    return Response(result)


@api_view(['PUT'])
def update_settings(request):
    """更新 .env 文件。"""
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent.parent / '.env'
    if not env_path.exists():
        return Response({'error': '.env 文件不存在'}, status=400)

    updates = request.data
    if not updates:
        return Response({'error': '无更新内容'}, status=400)

    lines = env_path.read_text().splitlines()
    existing_keys = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            existing_keys[key] = i

    for key, value in updates.items():
        if key.startswith('_'):
            logger.debug(f"update_settings: 跳过内部键 {key}")
            continue
        if isinstance(value, list):
            value = ','.join(str(v) for v in value)
        if key in existing_keys:
            lines[existing_keys[key]] = f'{key}={value}'
        else:
            lines.append(f'{key}={value}')

    env_path.write_text('\n'.join(lines) + '\n')
    return Response({'message': '配置已更新，重启后端生效'})


@api_view(['GET'])
def get_industry_factors(request):
    """获取行业因子权重配置。"""
    rows = list(AIndustryFactorConfig.objects.values("industry_name", "factor_name", "weight"))
    if not rows:
        return Response({'industries': {}})

    result: dict[str, dict[str, dict]] = {}
    for r in rows:
        industry = r["industry_name"]
        factor = r["factor_name"]
        result.setdefault(industry, {})[factor] = {
            "weight": float(r["weight"]) if r["weight"] is not None else 0.0,
            "description": "",
        }
    return Response({'industries': result})


@api_view(['PUT'])
def update_industry_factors(request):
    """更新行业因子权重。"""
    from stocks.services.upsert import get_upsert_manager

    data = request.data
    records = []
    for industry, factors in data.items():
        for factor_name, info in factors.items():
            records.append({
                'industry_name': industry,
                'factor_name': factor_name,
                'weight': float(info.get('weight', 0) or 0),
            })

    if records:
        um = get_upsert_manager()
        um.upsert(AIndustryFactorConfig, records, unique_keys=["industry_name", "factor_name"])
        um.flush()
    return Response({'message': f'已更新 {len(records)} 条配置'})


@api_view(['GET'])
def get_all_industries(request):
    """获取申万一级行业列表。"""
    try:
        industries = list(
            AIndustryClass.objects.filter(
                src="SW2021", level="L1", index_name__isnull=False,
            ).exclude(index_name="")
            .values_list("index_name", flat=True)
            .distinct()
            .order_by("index_name")
        )
    except Exception as e:
        logger.warning(f"get_all_industries: 查询失败: {e}")
        industries = []
    return Response({'industries': industries})


@api_view(['POST'])
def init_database(request):
    """init_database 已迁移：A 股表通过 scripts/migrate_ashare_schema.sql 管理。"""
    return Response({'message': 'A 股表由 scripts/migrate_ashare_schema.sql 管理，请手动执行 psql -f。'})
