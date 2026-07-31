"""Bubblemaps API client for wallet cluster analysis and supply bundling detection.

Bubblemaps visualizes on-chain wallet relationships.  Its Data API exposes
cluster information that reveals whether top holders are connected (bundled).

Endpoint: GET https://api.bubblemaps.io/v0/tokens/map/{chain}/{token_address}
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from memecoin_alert_bot.utils.helpers import fetch_json, is_valid_api_key

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bubblemaps.io/v0"


class BubblemapsClient:
    """Async wrapper for the Bubblemaps Data API."""

    def __init__(
        self,
        api_key: str = "",
        session: aiohttp.ClientSession | None = None,
    ):
        self.api_key = api_key if is_valid_api_key(api_key) else ""
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["x-api-key"] = api_key
        self._owned_session = session is None
        self.session = session or aiohttp.ClientSession(headers=headers)

    async def close(self) -> None:
        if self._owned_session and not self.session.closed:
            await self.session.close()

    async def fetch_token_map(self, chain: str, token: str) -> dict[str, Any] | None:
        """Fetch the full bubble-map data for a token."""
        url = f"{BASE_URL}/tokens/map/{chain}/{token}"
        return await fetch_json(self.session, url, timeout=20)

    async def fetch_chains(self) -> list[dict[str, Any]]:
        """Fetch the list of supported chains."""
        data = await fetch_json(self.session, f"{BASE_URL}/chains", timeout=15)
        return data if isinstance(data, list) else []

    def _compute_bundling(self, data: dict[str, Any]) -> dict[str, Any]:
        """Analyse cluster data and return bundling metrics.

        Returns a dict compatible with SafetyInfo / CoinData enrichment.
        """
        result: dict[str, Any] = {
            "safety": {
                "bundled_pct": 0.0,
                "bundled_wallets": 0,
                "is_bundled": False,
                "top_holders": [],
                "cluster_count": 0,
            },
            "sources": {"bubblemaps": data},
        }

        nodes = data.get("nodes", [])
        clusters = data.get("clusters", [])

        if not nodes or not clusters:
            return result

        # Sort holders by percentage descending.
        holders = sorted(
            [n for n in nodes if n.get("pct", 0) > 0],
            key=lambda n: n.get("pct", 0),
            reverse=True,
        )

        # Map node id → cluster id.
        node_to_cluster: dict[str, str] = {}
        for cluster_id, cluster_members in clusters.items():
            for member in cluster_members:
                if isinstance(member, str):
                    node_to_cluster[member] = cluster_id
                elif isinstance(member, dict):
                    node_to_cluster[member.get("id", "")] = cluster_id

        # Detect bundling: if top holders share clusters.
        top_holders = holders[:10]
        result["safety"]["top_holders"] = [
            {
                "address": h.get("id", ""),
                "pct": h.get("pct", 0),
                "is_fresh": h.get("isFreshWallet", False),
                "is_contract": h.get("isContract", False),
                "is_bundled": node_to_cluster.get(h.get("id", "")) in [
                    node_to_cluster.get(nh.get("id", ""))
                    for nh in holders[:3]
                ],
            }
            for h in top_holders
        ]

        # Cluster-based bundle detection.
        cluster_pct: dict[str, float] = {}
        for h in holders:
            cid = node_to_cluster.get(h.get("id", ""))
            if cid:
                cluster_pct[cid] = cluster_pct.get(cid, 0) + h.get("pct", 0)

        bundles = {cid: pct for cid, pct in cluster_pct.items() if pct > 10}
        if bundles:
            total_bundled = sum(bundles.values())
            result["safety"]["bundled_pct"] = total_bundled
            result["safety"]["bundled_wallets"] = sum(
                1 for h in holders if node_to_cluster.get(h.get("id", "")) in bundles
            )
            result["safety"]["is_bundled"] = total_bundled > 20

        result["safety"]["cluster_count"] = len(clusters)
        return result

    async def enrich_coin(self, mint: str) -> dict[str, Any]:
        """Return SafetyInfo enrichment from Bubblemaps cluster analysis."""
        if not self.api_key:
            return {"sources": {"bubblemaps": None}}
        data = await self.fetch_token_map("sol", mint)
        if not data:
            return {"sources": {"bubblemaps": None}}
        return self._compute_bundling(data)