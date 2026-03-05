"""Polymarket 工具函数。"""

# 排除的分类（回测/下载时自动 is_excluded=True）
EXCLUDED_CATEGORIES = {"sports", "pop-culture", "crypto"}

# 噪音 slug 模式：跟股市无关的博彩/娱乐/天气/统计类事件
NOISE_SLUG_PATTERNS = [
    "elon-musk-of-tweets",
    "donald-trump-of-truth-social-posts",
    "highest-temperature-in-",
    "1-free-app-in-the-us",
    "1-searched-person-on-google",
    "1-searched-passings-on-google",
    "1-searched-actor-on-google",
    "top-5-most-searched-people",
    "price-of-dozen-eggs",
    "starladder-budapest-major",
    "trump-approval-rating",
    "spx-up-or-down-on",
    "spx-opens-up-or-down",
    "what-will-powell-say-during",
    "what-will-trump-say-during",
    "what-will-trump-say-this-week",
    "who-will-attend-the-state-of-the-union",
    "game-awards-game-of-the-year",
    "lol-worlds",
    "winter-games-2026",
    "elon-musk-net-worth",
    "richest-person-on",
    "nothing-ever-happens",
    "how-many-jobs-added-in",
    "measles-cases-in-us",
]


def is_noise_slug(slug: str) -> bool:
    """检查 slug 是否匹配噪音模式。"""
    slug_lower = (slug or "").lower()
    return any(p in slug_lower for p in NOISE_SLUG_PATTERNS)


# 从 Gamma API tags 数组推导一级分类
# 优先级：匹配到第一个即返回
_TAG_TO_CATEGORY = {
    "politics": "politics",
    "elections": "politics",
    "global-elections": "politics",
    "world-elections": "politics",
    "us-election": "politics",
    "us-presidential-election": "politics",
    "geopolitics": "politics",
    "sports": "sports",
    "nba": "sports",
    "nfl": "sports",
    "soccer": "sports",
    "baseball": "sports",
    "mma": "sports",
    "basketball": "sports",
    "hockey": "sports",
    "tennis": "sports",
    "formula-1": "sports",
    "crypto": "crypto",
    "crypto-prices": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "economy": "economy",
    "fed-rates": "economy",
    "business": "economy",
    "pop-culture": "pop-culture",
    "entertainment": "pop-culture",
    "tech": "tech",
    "ai": "tech",
    "science": "science",
    "world": "world",
    "middle-east": "world",
}

# 按优先级排序（确保 politics > world, sports > 具体项目）
_CATEGORY_PRIORITY = [
    "politics", "economy", "crypto", "tech", "science",
    "world", "sports", "pop-culture",
]


def category_from_tags(tags: list[dict]) -> str:
    """从 Gamma API event.tags 推导一级分类。"""
    if not tags:
        return ""
    matched = set()
    for t in tags:
        slug = t.get("slug", "")
        cat = _TAG_TO_CATEGORY.get(slug)
        if cat:
            matched.add(cat)
    # 按优先级返回
    for cat in _CATEGORY_PRIORITY:
        if cat in matched:
            return cat
    return ""
