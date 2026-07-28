"""X (Twitter) API v2 client for narrative/viral detection."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twitter.com/2"


class XApiClient:
    """Minimal X API v2 search wrapper."""

    def __init__(self, bearer_token: str, session: aiohttp.ClientSession | None = None):
        self.bearer_token = bearer_token
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            }
        )

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def search_recent(
        self, query: str, max_results: int = 10
    ) -> dict[str, Any] | None:
        """Search recent public posts. Requires paid or elevated access."""
        if not self.bearer_token:
            return None
        url = (
            f"{BASE_URL}/tweets/search/recent"
            f"?query={query}&max_results={max_results}&tweet.fields=public_metrics"
        )
        return await fetch_json(self.session, url, timeout=15)

    async def narrative_mentions(self, symbol: str, name: str) -> dict[str, Any]:
        """Return simple engagement metrics for a coin's narrative."""
        result = {"count": 0, "engagement": 0, "sources": {"x_api": None}}
        # Fallback stub when no API key is configured.
        if not self.bearer_token:
            return result

        query = f"{symbol} OR {name} -is:retweet lang:en"
        data = await self.search_recent(query, max_results=10)
        result["sources"]["x_api"] = data
        if data and "data" in data:
            tweets = data["data"]
            result["count"] = len(tweets)
            result["engagement"] = sum(
                t.get("public_metrics", {}).get("impression_count", 0)
                + t.get("public_metrics", {}).get("like_count", 0) * 10
                + t.get("public_metrics", {}).get("retweet_count", 0) * 25
                for t in tweets
            )
        return result
