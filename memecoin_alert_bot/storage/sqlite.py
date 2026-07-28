"""SQLite-backed store for alerts and backtesting history."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from memecoin_alert_bot.engine.models import Alert, CoinData, Signal, ScoreResult

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    first_seen TEXT,
    last_seen TEXT,
    data TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    symbol TEXT,
    name TEXT,
    generated_at TEXT,
    primary_signal TEXT,
    verdict TEXT,
    risk TEXT,
    composite_score REAL,
    confidence REAL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS alert_cooldowns (
    mint TEXT PRIMARY KEY,
    last_alert_at TEXT
);

CREATE TABLE IF NOT EXISTS backtest_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    captured_at TEXT,
    market_cap REAL,
    price REAL,
    score REAL,
    payload TEXT
);
"""


class Storage:
    """Async SQLite storage layer."""

    def __init__(self, db_path: str = "memecoin_alert_bot.db"):
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database and run migrations."""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA)
        await self._connection.commit()
        logger.info("SQLite storage initialized at %s", self.db_path)

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def upsert_coin(self, coin: CoinData) -> None:
        """Insert or update a coin snapshot."""
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            """
            INSERT INTO coins (mint, symbol, name, first_seen, last_seen, data)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mint) DO UPDATE SET
                symbol=excluded.symbol,
                name=excluded.name,
                last_seen=excluded.last_seen,
                data=excluded.data
            """,
            (
                coin.mint,
                coin.symbol,
                coin.name,
                now,
                now,
                coin.model_dump_json(),
            ),
        )
        await self._connection.commit()

    async def get_coin(self, mint: str) -> CoinData | None:
        """Fetch the latest stored coin data."""
        async with self._connection.execute(
            "SELECT data FROM coins WHERE mint = ?", (mint,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return CoinData.model_validate_json(row["data"])

    async def save_alert(self, alert: Alert) -> None:
        """Persist a generated alert."""
        primary = alert.primary_signal
        await self._connection.execute(
            """
            INSERT INTO alerts
                (mint, symbol, name, generated_at, primary_signal, verdict, risk, composite_score, confidence, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.coin.mint,
                alert.coin.symbol,
                alert.coin.name,
                alert.generated_at.isoformat(),
                primary,
                alert.score.verdict.value,
                alert.score.risk.value,
                alert.score.composite_score,
                alert.score.confidence,
                alert.model_dump_json(),
            ),
        )
        await self._connection.commit()

    async def is_on_cooldown(self, mint: str, seconds: int) -> bool:
        """Return True if an alert was sent for this mint within `seconds`."""
        row = await self._connection.execute_fetchall(
            "SELECT last_alert_at FROM alert_cooldowns WHERE mint = ?", (mint,)
        )
        if not row:
            return False
        last = datetime.fromisoformat(row[0]["last_alert_at"])
        return (datetime.now(timezone.utc) - last).total_seconds() < seconds

    async def set_cooldown(self, mint: str) -> None:
        """Mark the current time as the last alert for a mint."""
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            """
            INSERT INTO alert_cooldowns (mint, last_alert_at)
            VALUES (?, ?)
            ON CONFLICT(mint) DO UPDATE SET last_alert_at = excluded.last_alert_at
            """,
            (mint, now),
        )
        await self._connection.commit()

    async def record_backtest_snapshot(self, coin: CoinData, score: ScoreResult) -> None:
        """Store a point-in-time snapshot for later PnL comparison."""
        await self._connection.execute(
            """
            INSERT INTO backtest_snapshots (mint, captured_at, market_cap, price, score, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                coin.mint,
                datetime.now(timezone.utc).isoformat(),
                coin.market_cap,
                coin.price,
                score.composite_score,
                json.dumps(
                    {
                        "coin": coin.model_dump(),
                        "score": score.model_dump(),
                    }
                ),
            ),
        )
        await self._connection.commit()

    async def recent_alerts(
        self, limit: int = 20, verdict: str | None = None
    ) -> list[dict[str, Any]]:
        """Return recent alerts with optional verdict filter."""
        query = "SELECT * FROM alerts"
        params = ()
        if verdict:
            query += " WHERE verdict = ?"
            params = (verdict,)
        query += " ORDER BY generated_at DESC LIMIT ?"
        params += (limit,)
        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
