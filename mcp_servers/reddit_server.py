import logging
import os
from collections import Counter

import praw
from mcp.server.fastmcp import FastMCP
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)
app = FastMCP("reddit-mcp")
_sentiment = SentimentIntensityAnalyzer()


def _get_reddit() -> praw.Reddit:
    return praw.Reddit(
        client_id=os.environ.get("REDDIT_CLIENT_ID", ""),
        client_secret=os.environ.get("REDDIT_CLIENT_SECRET", ""),
        user_agent="FinSight Research (by /u/finsight_bot)",
    )


@app.tool()
async def get_ticker_posts(
    ticker: str,
    subreddits: list[str] | None = None,
    days_back: int = 7,
    limit: int = 100,
) -> dict:
    """Fetch Reddit posts mentioning a ticker and compute sentiment"""
    subreddits = subreddits or ["stocks", "investing", "wallstreetbets"]
    reddit = _get_reddit()
    all_posts = []
    scores = []

    for sub_name in subreddits:
        try:
            sub = reddit.subreddit(sub_name)
            for post in sub.search(
                ticker, sort="new", time_filter="month", limit=limit // len(subreddits)
            ):
                combined = f"{post.title} {post.selftext}"
                vs = _sentiment.polarity_scores(combined)
                scores.append(vs["compound"])
                all_posts.append({
                    "subreddit": sub_name,
                    "title": post.title,
                    "score": post.score,
                    "num_comments": post.num_comments,
                    "url": post.url,
                    "created_utc": post.created_utc,
                    "sentiment": vs["compound"],
                })
        except Exception as e:
            logger.warning("Error fetching r/%s: %s", sub_name, e)

    avg_sentiment = sum(scores) / len(scores) if scores else 0.0
    pos_count = sum(1 for s in scores if s > 0.05)
    neg_count = sum(1 for s in scores if s < -0.05)

    return {
        "ticker": ticker,
        "total_posts": len(all_posts),
        "sentiment_score": round(avg_sentiment, 4),
        "positive_posts": pos_count,
        "negative_posts": neg_count,
        "neutral_posts": len(scores) - pos_count - neg_count,
        "posts": all_posts[:20],
    }


_starlette_app = None


def get_app():
    global _starlette_app
    if _starlette_app is None:
        _starlette_app = app.sse_app()
    return _starlette_app


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(get_app(), host="0.0.0.0", port=8030)
