"""Rate-limiter instances and TTL-cache singletons shared across all tool modules."""

from shared.rate_limiter import TokenBucket
from shared.redis_cache import make_cache

# ──────────────────────────────────────────────
# Rate limiters
# ──────────────────────────────────────────────
_YF_LIMITER = TokenBucket(rate=4, burst=8)  # Yahoo Finance: no published cap, conservative
_EDGAR_LIMITER = TokenBucket(rate=8, burst=10)  # SEC: 10 req/s hard cap; stay below
_NEWS_LIMITER = TokenBucket(rate=2, burst=4)  # RSS + Yahoo news fallback
_SEARCH_LIMITER = TokenBucket(rate=2, burst=4)  # Yahoo Finance ticker search
_SANDBOX_LIMITER = TokenBucket(rate=4, burst=8)  # sandbox subprocess spawns
_WEB_SEARCH_LIMITER = TokenBucket(rate=1, burst=3)  # DuckDuckGo web search

# ──────────────────────────────────────────────
# TTL caches
# ──────────────────────────────────────────────
cache_prices = make_cache(60, "prices")  # 1 min — intraday prices
cache_benchmark = make_cache(3600, "benchmark")  # 1 hr — index benchmarks
cache_financials = make_cache(3600, "financials")  # 1 hr — quarterly data
cache_news = make_cache(300, "news")  # 5 min — news headlines
cache_filing = make_cache(None, "filing", max_entries=200)  # permanent LRU-200
cache_submissions = make_cache(21600, "submissions")  # 6 hr — submission lists
cache_macro = make_cache(900, "macro")  # 15 min — macro moves slowly
cache_peers = make_cache(86400, "peers")  # 24 hr — peers stable intraday
cache_shocks = make_cache(604800, "shocks")  # 7 days — historical shocks
cache_web_search = make_cache(300, "web_search")  # 5 min — web search results

BENCHMARK_TICKERS = frozenset({"^GSPC", "^IXIC", "^DJI", "^RUT", "^VIX", "DXY"})
