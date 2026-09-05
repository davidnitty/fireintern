"""Helper functions used across the project."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
    retries: int = 1,
    retry_delay: float = 1.0,
) -> dict[str, Any] | None:
    """Fetch JSON from an HTTP endpoint with error handling and retries.

    Intermediate failures are logged at DEBUG; only a total failure after
    all retries is logged at WARNING.
    """
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.debug("Non-200 response from %s: %s %s", url, resp.status, text[:200])
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"Timeout fetching {url}")
        except Exception as exc:
            last_error = exc
        logger.debug("Request to %s failed (attempt %d/%d): %s", url, attempt + 1, retries, last_error)
        if attempt < max(1, retries) - 1:
            await asyncio.sleep(retry_delay * (attempt + 1))
    logger.warning("Error fetching %s: %s", url, last_error)
    return None


@asynccontextmanager
async def http_session(headers: dict[str, str] | None = None):
    """Async context manager for aiohttp session."""
    session = aiohttp.ClientSession(headers=headers)
    try:
        yield session
    finally:
        await session.close()


def shorten_address(address: str, chars: int = 4) -> str:
    """Shorten a Solana address for display."""
    if len(address) <= chars * 2 + 2:
        return address
    return f"{address[:chars]}...{address[-chars:]}"


def format_currency(value: float | None, prefix: str = "$") -> str:
    """Format a number as compact currency."""
    if value is None:
        return "N/A"
    if value >= 1_000_000_000:
        return f"{prefix}{value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{prefix}{value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"{prefix}{value/1_000:.2f}K"
    return f"{prefix}{value:.4f}"


def setup_logging(level: str = "INFO") -> None:
    """Configure standard logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def is_valid_api_key(key: str) -> bool:
    """Return False for empty or placeholder API keys."""
    if not key or not key.strip():
        return False
    placeholder_prefixes = ("your_", "replace_", "changeme", "TODO", "xxx")
    return not key.strip().lower().startswith(placeholder_prefixes)


def next_moon_threshold(update_pct: float, last_multiple: float) -> float:
    """Cumulative multiple required for the next moon update.

    First update fires at ``1 + update_pct%`` above the original call
    (e.g. 50% => 1.5X). Every subsequent update fires when the token
    **doubles** from the last announced level (20.5X -> 41X -> 82X ...),
    matching the expected cumulative behaviour.
    """
    first = 1.0 + update_pct / 100.0
    if last_multiple <= 1.0:
        return first
    return last_multiple * 2.0


def moon_check_interval_minutes(age_minutes: float) -> float:
    """How often to re-check an alerted token's price, by alert age.

    Fresh alerts are checked every 30s (fast pumps). The cadence decays —
    30s for the first 30 minutes, 2 minutes up to 2 hours, then 10 minutes
    up to the 24-hour horizon — so slow, hours-long runners like a
    $55k -> $3.7M climb are still caught well inside the move.
    """
    if age_minutes < 30:
        return 0.5
    if age_minutes < 120:
        return 2.0
    return 10.0


async def fetch_metadata_json(
    session: aiohttp.ClientSession,
    uri: str,
    timeout: int = 10,
) -> dict[str, Any] | None:
    """Fetch a Metaplex/IPFS metadata JSON document and return name/symbol.

    Handles ``ipfs://`` URIs by rewriting them to a public gateway. Returns
    None on any failure — callers must treat metadata as optional.
    """
    if not uri:
        return None
    url = uri
    if url.startswith("ipfs://"):
        url = "https://ipfs.io/ipfs/" + url[len("ipfs://"):].lstrip("/")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("Metadata fetch failed for %s: %s", uri, exc)
        return None
    if not isinstance(data, dict):
        return None
    return {
        "name": str(data.get("name") or "").strip(),
        "symbol": str(data.get("symbol") or "").strip(),
        "description": str(data.get("description") or "").strip(),
    }
