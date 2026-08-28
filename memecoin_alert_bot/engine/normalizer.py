"""Normalize heterogeneous data source responses into CoinData."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from memecoin_alert_bot.engine.models import CoinData, SafetyInfo, TopHolder


def _coalesce(*values: Any):
    """Return first non-None value."""
    for v in values:
        if v is not None:
            return v
    return None


def _parse_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _parse_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def create_from_pumpportal(event: dict[str, Any], sol_usd: float = 170.0) -> CoinData:
    """Create a baseline CoinData from a PumpPortal create event.

    PumpPortal reports ``marketCapSol`` in SOL units. Convert to USD so the
    market-cap floor (USD) compares like with like; enrichment overwrites
    with the provider's own USD market cap when available.
    """
    market_cap_sol = _parse_float(event.get("marketCapSol", 0)) or 0.0
    return CoinData(
        mint=event.get("mint") or event.get("token") or "",
        symbol=event.get("symbol", "UNKNOWN"),
        name=event.get("name", ""),
        description=event.get("description", ""),
        dev_wallet=event.get("traderPublicKey", ""),
        metadata_uri=event.get("uri"),
        price_sol=_parse_float(event.get("initialBuy", 0)),
        market_cap=market_cap_sol * sol_usd,
        age_seconds=0,
        sources={"pumpportal": event},
    )


def merge_enrichment(coin: CoinData, enrichment: dict[str, Any]) -> CoinData:
    """Merge a single enrichment dict into an existing CoinData."""
    data = coin.model_dump()

    def _merge_safety(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = {**existing}
        for k, v in incoming.items():
            if k == "top_holders":
                merged.setdefault("top_holders", [])
                # Prefer non-empty incoming holders.
                if v:
                    merged["top_holders"] = v
            elif v is not None:
                merged[k] = v
        return merged

    # Simple scalar merge
    for key in [
        "symbol",
        "name",
        "description",
        "dev_wallet",
        "website",
        "twitter",
        "telegram",
    ]:
        if enrichment.get(key) is not None:
            data[key] = enrichment[key]

    for key in [
        "market_cap",
        "volume_24h",
        "liquidity",
        "price",
        "price_sol",
        "holders",
        "age_seconds",
        "buy_volume_1h",
        "sell_volume_1h",
        "buy_pressure",
        "volume_5m",
        "volume_1h",
        "buys_5m",
        "sells_5m",
        "buys_1h",
        "sells_1h",
        "price_change_5m",
        "price_change_1h",
        "vl_ratio_1h",
        "flow_ratio",
        "swap_speed",
        "flow_label",
    ]:
        if enrichment.get(key) is not None:
            data[key] = enrichment[key]

    # Verified USD flow data must not be overwritten by lower-quality chain
    # native or inferred data from a subsequent source.
    incoming_quality = enrichment.get("flow_data_quality")
    if incoming_quality:
        existing_quality = data.get("flow_data_quality", "unknown")
        quality_rank = {"unknown": 0, "directional_only": 1, "verified_usd": 2}
        if quality_rank.get(incoming_quality, 0) >= quality_rank.get(existing_quality, 0):
            data["flow_data_quality"] = incoming_quality

    if "tokenized_agent" in enrichment:
        data["tokenized_agent"] = data.get("tokenized_agent", False) or bool(
            enrichment["tokenized_agent"]
        )

    if "social_links" in enrichment:
        data.setdefault("social_links", {}).update(enrichment["social_links"])
        for k in ["twitter", "telegram", "website"]:
            if enrichment["social_links"].get(k):
                data[k] = enrichment["social_links"][k]

    if "safety" in enrichment:
        data["safety"] = _merge_safety(data.get("safety", {}), enrichment["safety"])

    # Merge sources
    data.setdefault("sources", {}).update(enrichment.get("sources", {}))

    # Re-parse safety
    data["safety"] = SafetyInfo(**data["safety"]).model_dump()
    # Re-parse top holders
    data["safety"]["top_holders"] = [
        TopHolder(**h).model_dump() for h in data["safety"].get("top_holders", [])
    ]

    # Parse agent/AI keywords
    name_desc = f"{data.get('name', '')} {data.get('description', '')}".lower()
    ai_kw = ["autonomous", "ai", "agent", "agent token", "ai trading", "no human"]
    data["ai_keywords"] = [k for k in ai_kw if k in name_desc]
    data["tokenized_agent"] = data.get("tokenized_agent", False) or bool(data["ai_keywords"])

    # Ensure created_at stays the same if coin already existed
    if isinstance(coin.created_at, datetime):
        data["created_at"] = coin.created_at
    else:
        data["created_at"] = datetime.now(timezone.utc)

    return CoinData(**data)
