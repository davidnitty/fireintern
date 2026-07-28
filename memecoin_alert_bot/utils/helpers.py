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
) -> dict[str, Any] | None:
    """Fetch JSON from an HTTP endpoint with error handling."""
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
        logger.warning("Timeout fetching %s", url)
    except Exception as exc:
        logger.warning("Error fetching %s: %s", url, exc)
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
