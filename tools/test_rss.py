import feedparser
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

feeds = {
    "yahoo_finance": "https://finance.yahoo.com/news/rssindex",
    "cnbc_top": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories",
    "seeking_alpha": "https://seekingalpha.com/feed.xml",
}
for name, url in feeds.items():
    try:
        f = feedparser.parse(url)
        print(f"{name}: status={f.status}, entries={len(f.entries)}")
        for e in f.entries[:3]:
            t = e.get("title", "")[:80]
            print(f"  - {t}")
    except Exception as e:
        print(f"{name}: ERROR {e}")
