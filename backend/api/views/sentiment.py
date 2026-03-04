"""Sentiment scraping API views."""
import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from backend.services.data.database import DatabaseManager
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['GET'])
def sentiment_status(request):
    """Get article counts and date ranges by source (including all registered scrapers + research reports)."""
    db = _get_db()

    tier_map = {
        'gov_cn': 1, 'xinhua': 1, 'people': 1, 'cctv': 1,
        'ndrc': 2, 'miit': 2, 'mofcom': 2, 'cninfo': 2,
        'csrc': 3, 'pbc': 3, 'nfra': 3,
        'nea': 4, 'mohurd': 4,
        'twitter_trump': 5, 'twitter_vance': 5, 'twitter_rubio': 5,
        'eastmoney': 6, 'cls': 6, 'sina': 6,
        'research_report': 7,
        'polymarket': 8,
    }
    tier_names = {
        1: '最高层', 2: '产业层', 3: '金融监管', 4: '专项行业', 5: '美国政策',
        6: '财经媒体', 7: '券商研报', 8: '预测市场',
    }
    source_labels = {
        'gov_cn': '中国政府网', 'xinhua': '新华社', 'people': '人民日报',
        'cctv': 'CCTV新闻联播', 'ndrc': '发改委', 'miit': '工信部',
        'mofcom': '商务部', 'cninfo': '巨潮公告', 'csrc': '证监会',
        'pbc': '人民银行', 'nfra': '金融监管总局', 'nea': '能源局',
        'mohurd': '住建部', 'twitter_trump': 'Trump', 'twitter_vance': 'Vance',
        'twitter_rubio': 'Rubio', 'eastmoney': '东方财富', 'cls': '财联社',
        'sina': '新浪财经', 'research_report': '券商研报',
        'polymarket': 'Polymarket',
    }

    # Query policy_article counts
    db_counts = {}  # source -> {cnt, earliest, latest}
    try:
        df = db.query(
            "SELECT source, COUNT(*) as cnt, "
            "MIN(publish_date) as earliest, MAX(publish_date) as latest "
            "FROM policy_article GROUP BY source ORDER BY source"
        )
        if not df.empty:
            for _, row in df.iterrows():
                db_counts[row['source']] = {
                    'cnt': int(row['cnt']),
                    'earliest': str(row['earliest']) if row['earliest'] else None,
                    'latest': str(row['latest']) if row['latest'] else None,
                }
    except Exception:
        pass

    # Query research_report counts
    try:
        rr_df = db.query(
            "SELECT COUNT(*) as cnt, MIN(report_date) as earliest, MAX(report_date) as latest "
            "FROM research_report"
        )
        if not rr_df.empty and rr_df['cnt'].iloc[0] > 0:
            db_counts['research_report'] = {
                'cnt': int(rr_df['cnt'].iloc[0]),
                'earliest': str(rr_df['earliest'].iloc[0]) if rr_df['earliest'].iloc[0] else None,
                'latest': str(rr_df['latest'].iloc[0]) if rr_df['latest'].iloc[0] else None,
            }
    except Exception:
        pass

    # Build sources list: all registered scrapers + research_report, even if 0
    sources = []
    total = 0
    for src_id in tier_map:
        info = db_counts.get(src_id, {'cnt': 0, 'earliest': None, 'latest': None})
        t = tier_map[src_id]
        sources.append({
            'source': src_id,
            'label': source_labels.get(src_id, src_id),
            'tier': t,
            'tier_name': tier_names.get(t, '未知'),
            'count': info['cnt'],
            'earliest': info['earliest'],
            'latest': info['latest'],
        })
        total += info['cnt']

    # Distinct categories
    categories = []
    try:
        cat_df = db.query(
            "SELECT DISTINCT category FROM policy_article "
            "WHERE category IS NOT NULL AND category != '' ORDER BY category"
        )
        categories = cat_df['category'].tolist() if not cat_df.empty else []
    except Exception:
        pass

    return Response({'sources': sources, 'total': total, 'categories': categories})


@api_view(['GET'])
def sentiment_articles(request):
    """Get articles with pagination and filtering by source/date."""
    db = _get_db()
    source = request.query_params.get('source')
    category = request.query_params.get('category')
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    keyword = request.query_params.get('keyword')
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 20))
    offset = (page - 1) * page_size

    # research_report 走独立查询
    if source == 'research_report':
        return _query_research_reports(db, start_date, end_date, keyword, page, page_size, offset)

    # Build WHERE clause with parameterized queries (prevent SQL injection)
    conditions = []
    params = {}
    if source:
        conditions.append("source = :source")
        params['source'] = source
    if category:
        conditions.append("category = :category")
        params['category'] = category
    if start_date:
        conditions.append("publish_date >= :start_date")
        params['start_date'] = start_date
    if end_date:
        conditions.append("publish_date <= :end_date")
        params['end_date'] = end_date
    if keyword:
        conditions.append("title LIKE :keyword")
        params['keyword'] = f'%{keyword}%'

    where = ''
    if conditions:
        where = 'WHERE ' + ' AND '.join(conditions)

    try:
        total_df = db.query(
            f"SELECT COUNT(*) as cnt FROM policy_article {where}",
            params=params,
        )
        total = int(total_df['cnt'].iloc[0])

        df = db.query(
            f"SELECT source, tier, title, url, publish_date, category, summary, scraped_at "
            f"FROM policy_article {where} "
            f"ORDER BY publish_date DESC LIMIT :limit OFFSET :offset",
            params={**params, 'limit': page_size, 'offset': offset},
        )
    except Exception:
        return Response({'articles': [], 'total': 0, 'page': page, 'page_size': page_size})

    articles = df.to_dict('records') if not df.empty else []
    for a in articles:
        if a.get('publish_date'):
            a['publish_date'] = str(a['publish_date'])
        if a.get('scraped_at'):
            a['scraped_at'] = str(a['scraped_at'])

    return Response({
        'articles': articles,
        'total': total,
        'page': page,
        'page_size': page_size,
    })


def _query_research_reports(db, start_date, end_date, keyword, page, page_size, offset):
    """Query research_report table and return in the same format as policy_article."""
    conditions = []
    params = {}
    if start_date:
        conditions.append("report_date >= :start_date")
        params['start_date'] = start_date
    if end_date:
        conditions.append("report_date <= :end_date")
        params['end_date'] = end_date
    if keyword:
        conditions.append("title LIKE :keyword")
        params['keyword'] = f'%{keyword}%'

    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''

    try:
        total_df = db.query(
            f"SELECT COUNT(*) as cnt FROM research_report {where}",
            params=params,
        )
        total = int(total_df['cnt'].iloc[0])

        df = db.query(
            f"SELECT 'research_report' as source, 7 as tier, "
            f"CONCAT('[', institution, '] ', title) as title, "
            f"CASE WHEN info_code IS NOT NULL AND info_code != '' "
            f"  THEN CONCAT('https://data.eastmoney.com/report/zw_stock.jshtml?infocode=', info_code) "
            f"  ELSE NULL END as url, "
            f"report_date as publish_date, "
            f"CONCAT(rating, CASE WHEN rating_score IS NOT NULL THEN CONCAT('(', rating_score, ')') ELSE '' END) as category, "
            f"CONCAT(analyst, ' - ', institution) as summary, "
            f"updated_at as scraped_at "
            f"FROM research_report {where} "
            f"ORDER BY report_date DESC LIMIT :limit OFFSET :offset",
            params={**params, 'limit': page_size, 'offset': offset},
        )
    except Exception:
        return Response({'articles': [], 'total': 0, 'page': page, 'page_size': page_size})

    articles = df.to_dict('records') if not df.empty else []
    for a in articles:
        if a.get('publish_date'):
            a['publish_date'] = str(a['publish_date'])
        if a.get('scraped_at'):
            a['scraped_at'] = str(a['scraped_at'])

    return Response({
        'articles': articles,
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@api_view(['POST'])
def sentiment_download(request):
    """Start sentiment scraping task.

    Body params:
        source (str, optional): 单个来源标识，如 "cctv"、"cninfo"。
        tier (int, optional): 层级，1~5。
        max_pages (int, optional): 最大翻页数/回看天数。
        incremental (bool, optional): 增量模式，自动计算回看天数（默认 false）。
        backfill (bool, optional): 全量补录模式，使用 SENTIMENT_BACKFILL_DAYS 天（默认 false）。
    """
    source = request.data.get('source')
    tier = request.data.get('tier')
    max_pages = request.data.get('max_pages')
    incremental = bool(request.data.get('incremental', False))
    backfill = bool(request.data.get('backfill', False))
    max_pages = int(max_pages) if max_pages else None

    if source:
        task_id = task_manager.submit(
            f'抓取舆情-{source}', _run_download_source, source, max_pages, incremental, backfill,
        )
    elif tier:
        task_id = task_manager.submit(
            f'抓取舆情-层级{tier}', _run_download_tier, int(tier), max_pages, incremental, backfill,
        )
    else:
        task_id = task_manager.submit(
            '全量抓取舆情', _run_download_all, max_pages, incremental, backfill,
        )

    return Response({'task_id': task_id})


def _run_download_source(task_id, source, max_pages=None, incremental=False, backfill=False):
    from backend.services.sentiment.downloader import SentimentDownloader
    from backend.services.config import SENTIMENT_MAX_PAGES, SENTIMENT_BACKFILL_DAYS
    db = _get_db()
    dl = SentimentDownloader(db)
    if backfill:
        pages = max_pages or SENTIMENT_BACKFILL_DAYS
        mode_label = f'全量补录 {pages} 天'
    elif incremental:
        pages = max_pages  # None → downloader 自动计算
        mode_label = '增量'
    else:
        pages = max_pages or SENTIMENT_MAX_PAGES
        mode_label = f'max_pages={pages}'
    task_manager.update_progress(task_id, 20, f'抓取 {source} ({mode_label})...')
    result = dl.download_source(source, max_pages=pages or SENTIMENT_MAX_PAGES, incremental=incremental)
    return result


def _run_download_tier(task_id, tier, max_pages=None, incremental=False, backfill=False):
    from backend.services.sentiment.downloader import SentimentDownloader
    from backend.services.config import SENTIMENT_MAX_PAGES, SENTIMENT_BACKFILL_DAYS
    db = _get_db()
    dl = SentimentDownloader(db)
    if backfill:
        pages = max_pages or SENTIMENT_BACKFILL_DAYS
    elif incremental:
        pages = max_pages
    else:
        pages = max_pages or SENTIMENT_MAX_PAGES
    task_manager.update_progress(task_id, 20, f'抓取层级 {tier}...')
    results = dl.download_tier(tier, max_pages=pages or SENTIMENT_MAX_PAGES, incremental=incremental)
    return {k: v for k, v in results.items()}


def _run_download_all(task_id, max_pages=None, incremental=False, backfill=False):
    from backend.services.sentiment.downloader import SentimentDownloader
    from backend.services.config import SENTIMENT_MAX_PAGES, SENTIMENT_BACKFILL_DAYS
    db = _get_db()
    dl = SentimentDownloader(db)
    if backfill:
        pages = max_pages or SENTIMENT_BACKFILL_DAYS
    elif incremental:
        pages = max_pages
    else:
        pages = max_pages or SENTIMENT_MAX_PAGES
    task_manager.update_progress(task_id, 10, f'全量抓取舆情...')
    results = dl.download_all(max_pages=pages or SENTIMENT_MAX_PAGES, incremental=incremental)
    total_new = sum(r['new'] for r in results.values())
    return {'total_new': total_new, 'sources': {k: v for k, v in results.items()}}


@api_view(['POST'])
def sentiment_analyze(request):
    """Trigger sentiment analysis on pending articles."""
    from backend.services.sentiment.analyzer import SentimentAnalyzer
    max_articles = int(request.data.get('max_articles', 500))
    task_id = task_manager.submit('舆情分析', _run_analyze, max_articles)
    return Response({'task_id': task_id})


def _run_analyze(task_id, max_articles):
    from backend.services.sentiment.analyzer import SentimentAnalyzer
    db = _get_db()
    analyzer = SentimentAnalyzer(db)
    task_manager.update_progress(task_id, 20, '分析文章...')
    result = analyzer.analyze_pending(max_articles=max_articles)
    return result


@api_view(['GET'])
def sentiment_analysis_stats(request):
    """Get sentiment analysis statistics."""
    from backend.services.sentiment.analyzer import SentimentAnalyzer
    db = _get_db()
    analyzer = SentimentAnalyzer(db)
    stats = analyzer.get_analysis_stats()
    return Response(stats)


@api_view(['POST'])
def sentiment_download_and_analyze(request):
    """Download sentiment + auto-analyze."""
    source = request.data.get('source')
    task_id = task_manager.submit('抓取+分析舆情', _run_download_and_analyze, source)
    return Response({'task_id': task_id})


def _run_download_and_analyze(task_id, source=None):
    from backend.services.sentiment.downloader import SentimentDownloader
    from backend.services.sentiment.analyzer import SentimentAnalyzer
    db = _get_db()

    # Phase 1: Download
    task_manager.update_progress(task_id, 10, '抓取舆情...')
    dl = SentimentDownloader(db)
    if source:
        dl.download_source(source)
    else:
        dl.download_all()

    # Phase 2: Analyze
    task_manager.update_progress(task_id, 60, '舆情分析 (keyword + LLM)...')
    analyzer = SentimentAnalyzer(db)
    result = analyzer.analyze_pending(max_articles=1000)
    return result


@api_view(['POST'])
def sentiment_backfill_analyze(request):
    """Backfill: loop-analyze all unanalyzed articles."""
    task_id = task_manager.submit('补录舆情分析', _run_backfill_analyze)
    return Response({'task_id': task_id})


def _run_backfill_analyze(task_id):
    from backend.services.sentiment.analyzer import SentimentAnalyzer
    db = _get_db()
    analyzer = SentimentAnalyzer(db)
    total_kw = 0
    total_llm = 0
    batch = 0

    while True:
        batch += 1
        task_manager.update_progress(task_id, min(90, batch * 10), f'补录第 {batch} 批...')
        result = analyzer.analyze_pending(max_articles=500)
        total_kw += result["keyword_analyzed"]
        total_llm += result["llm_analyzed"]
        if result["keyword_analyzed"] == 0 and result["llm_analyzed"] == 0:
            break

    return {"keyword_analyzed": total_kw, "llm_analyzed": total_llm}


@api_view(['POST'])
def sentiment_backfill_content(request):
    """Backfill: fetch article body text for articles with empty content."""
    source = request.data.get('source')
    task_id = task_manager.submit(
        f'补录全文{"-" + source if source else ""}',
        _run_backfill_content,
        source,
    )
    return Response({'task_id': task_id})


def _run_backfill_content(task_id, source=None):
    from backend.services.sentiment.downloader import SentimentDownloader
    db = _get_db()
    dl = SentimentDownloader(db)
    total_success = 0
    total_failed = 0
    total_count = 0
    batch = 0

    while True:
        batch += 1
        task_manager.update_progress(
            task_id, min(90, batch * 10),
            f'补录第 {batch} 批 (已完成 {total_success} 篇)...',
        )
        result = dl.backfill_content(source=source, batch_size=100)
        total_count += result["total"]
        total_success += result["success"]
        total_failed += result["failed"]
        if result["total"] == 0:
            break

    return {"total": total_count, "success": total_success, "failed": total_failed}


@api_view(['POST'])
def sentiment_backfill_llm(request):
    """Backfill: run LLM analysis on all eligible articles (keyword intensity >= threshold)."""
    task_id = task_manager.submit('补录LLM打分', _run_backfill_llm)
    return Response({'task_id': task_id})


def _run_backfill_llm(task_id):
    from backend.services.sentiment.analyzer import SentimentAnalyzer
    db = _get_db()
    analyzer = SentimentAnalyzer(db)
    logger.info('补录LLM打分: 开始（先补关键词分析，再补LLM）')
    total_kw = 0
    total_llm = 0
    batch = 0

    while True:
        batch += 1
        logger.info(f'补录LLM打分: 第 {batch} 批, 关键词已完成 {total_kw} 篇, LLM已完成 {total_llm} 篇')
        task_manager.update_progress(
            task_id, min(90, batch * 5),
            f'第 {batch} 批 (关键词 {total_kw}, LLM {total_llm})...',
        )
        result = analyzer.analyze_pending(max_articles=500)
        logger.info(f'补录LLM打分: 第 {batch} 批结果 = {result}')
        total_kw += result["keyword_analyzed"]
        total_llm += result["llm_analyzed"]
        if result["keyword_analyzed"] == 0 and result["llm_analyzed"] == 0:
            break

    logger.info(f'补录LLM打分: 完成, 关键词 {total_kw} 篇, LLM {total_llm} 篇')
    return {"keyword_analyzed": total_kw, "llm_analyzed": total_llm}
