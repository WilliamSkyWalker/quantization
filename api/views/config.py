"""System configuration API views."""
import logging
import os

from rest_framework.decorators import api_view
from rest_framework.response import Response

from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)

# Settings that are safe to expose (no tokens/passwords)
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

# Settings with sensitive values (show presence only)
SENSITIVE_SETTINGS = [
    'TUSHARE_TOKEN', 'TWITTER_USERNAME', 'TWITTER_EMAIL', 'TWITTER_PASSWORD',
    'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE',
]


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['GET'])
def get_settings(request):
    """Get current system settings (sensitive values masked)."""
    from services import config as qs

    result = {}
    for key in SAFE_SETTINGS:
        val = getattr(qs, key, None)
        result[key] = val

    # Sensitive: show if set
    sensitive = {}
    for key in SENSITIVE_SETTINGS:
        val = getattr(qs, key, None)
        if val:
            sensitive[key] = '***已配置***'
        else:
            sensitive[key] = ''
    result['_sensitive'] = sensitive

    return Response(result)


@api_view(['PUT'])
def update_settings(request):
    """Update .env file with new settings."""
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent.parent / '.env'

    if not env_path.exists():
        return Response({'error': '.env 文件不存在'}, status=400)

    updates = request.data
    if not updates:
        return Response({'error': '无更新内容'}, status=400)

    # Read existing .env
    lines = env_path.read_text().splitlines()
    existing_keys = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            existing_keys[key] = i

    # Update or append
    for key, value in updates.items():
        if key.startswith('_'):
            logger.debug(f"update_settings: 跳过内部键 {key}")
            continue
        # Convert list values to comma-separated string for .env
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
    """Get industry factor weight configuration."""
    db = _get_db()
    df = db.get_industry_factor_weights()
    if df.empty:
        return Response({'industries': {}})

    result = {}
    for industry, grp in df.groupby('industry_name'):
        factors = {}
        for _, row in grp.iterrows():
            factors[row['factor_name']] = {
                'weight': float(row['weight']),
                'description': row.get('description', ''),
            }
        result[industry] = factors

    return Response({'industries': result})


@api_view(['PUT'])
def update_industry_factors(request):
    """Update industry factor weights."""
    db = _get_db()
    data = request.data

    records = []
    for industry, factors in data.items():
        for factor_name, info in factors.items():
            records.append({
                'industry_name': industry,
                'factor_name': factor_name,
                'weight': info.get('weight', 0),
                'description': info.get('description', ''),
            })

    if records:
        db.upsert_industry_factor_config(records)

    return Response({'message': f'已更新 {len(records)} 条配置'})


@api_view(['GET'])
def get_all_industries(request):
    """Get all distinct Shenwan L1 industry names from industry_class table."""
    db = _get_db()
    try:
        df = db.query(
            "SELECT DISTINCT industry_name FROM industry_class "
            "WHERE industry_name IS NOT NULL AND industry_name != '' "
            "ORDER BY industry_name"
        )
        industries = df['industry_name'].tolist() if not df.empty else []
    except Exception as e:
        logger.warning(f"get_all_industries: 查询行业列表失败: {e}")
        industries = []
    return Response({'industries': industries})


@api_view(['POST'])
def init_database(request):
    """Initialize database tables."""
    db = _get_db()
    db.init_tables()
    return Response({'message': '数据库表结构已初始化'})
