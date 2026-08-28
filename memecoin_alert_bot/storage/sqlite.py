"""SQLite-backed store for alerts and backtesting history."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    chain TEXT,
    symbol TEXT,
    ts TEXT,
    stage TEXT,
    reason TEXT,
    market_cap REAL,
    score_json TEXT
);

CREATE TABLE IF NOT EXISTS alert_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER,
    mint TEXT,
    horizon_min INTEGER,
    alert_at TEXT,
    price_alert REAL,
    price_horizon REAL,
    mc_alert REAL,
    mc_horizon REAL,
    pct_change REAL,
    captured_at TEXT,
    UNIQUE(alert_id, horizon_min)
);

CREATE TABLE IF NOT EXISTS chain_state (
    chain TEXT PRIMARY KEY,
    last_block INTEGER,
    updated_at TEXT
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
                        "coin": coin.model_dump(mode="json"),
                        "score": score.model_dump(mode="json"),
                    }
                ),
            ),
        )
        await self._connection.commit()

    async def get_chain_state(self, chain: str) -> dict[str, Any] | None:
        """Fetch persisted chain scanner state."""
        async with self._connection.execute(
            "SELECT * FROM chain_state WHERE chain = ?", (chain,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def set_chain_state(self, chain: str, last_block: int) -> None:
        """Persist the last scanned block for a chain."""
        now = datetime.now(timezone.utc).isoformat()
        await self._connection.execute(
            """
            INSERT INTO chain_state (chain, last_block, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chain) DO UPDATE SET
                last_block = excluded.last_block,
                updated_at = excluded.updated_at
            """,
            (chain, last_block, now),
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

    # ── Backtesting ledger (guide §5) ────────────────────────────────────

    async def record_decision(
        self,
        mint: str,
        chain: str,
        symbol: str,
        stage: str,
        reason: str = "",
        market_cap: float | None = None,
        score_json: str | None = None,
    ) -> None:
        """Record one evaluation decision — sent, suppressed, or rejected."""
        await self._connection.execute(
            """
            INSERT INTO decisions (mint, chain, symbol, ts, stage, reason, market_cap, score_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint,
                chain,
                symbol,
                datetime.now(timezone.utc).isoformat(),
                stage,
                reason,
                market_cap,
                score_json,
            ),
        )
        await self._connection.commit()

    async def get_alerts_without_outcomes(self, horizons: list[int]) -> list[dict[str, Any]]:
        """Return (alert_id, mint, generated_at, payload, horizon) rows needing outcomes.

        A row is due when ``now >= alert_time + horizon`` and no outcome row
        exists yet for that (alert, horizon) pair.
        """
        now = datetime.now(timezone.utc)
        async with self._connection.execute(
            "SELECT id, mint, generated_at, payload FROM alerts ORDER BY id"
        ) as cursor:
            alerts = await cursor.fetchall()
        async with self._connection.execute(
            "SELECT alert_id, horizon_min FROM alert_outcomes"
        ) as cursor:
            existing = {(r["alert_id"], r["horizon_min"]) for r in await cursor.fetchall()}

        due: list[dict[str, Any]] = []
        for row in alerts:
            try:
                alert_at = datetime.fromisoformat(row["generated_at"])
            except (ValueError, TypeError):
                continue
            for horizon in horizons:
                if (row["id"], horizon) in existing:
                    continue
                due_at = alert_at + timedelta(minutes=horizon)
                if now >= due_at:
                    due.append(
                        {
                            "alert_id": row["id"],
                            "mint": row["mint"],
                            "generated_at": row["generated_at"],
                            "payload": row["payload"],
                            "horizon_min": horizon,
                        }
                    )
        return due

    async def record_outcome(
        self,
        alert_id: int,
        mint: str,
        horizon_min: int,
        alert_at: str,
        price_alert: float | None,
        price_horizon: float | None,
        mc_alert: float | None,
        mc_horizon: float | None,
    ) -> None:
        """Store one horizon outcome for an alert."""
        pct = None
        if price_alert and price_horizon and price_alert > 0:
            pct = (price_horizon / price_alert - 1) * 100
        await self._connection.execute(
            """
            INSERT OR IGNORE INTO alert_outcomes
                (alert_id, mint, horizon_min, alert_at, price_alert, price_horizon,
                 mc_alert, mc_horizon, pct_change, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                mint,
                horizon_min,
                alert_at,
                price_alert,
                price_horizon,
                mc_alert,
                mc_horizon,
                pct,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._connection.commit()

    async def calibration_summary(self) -> list[dict[str, Any]]:
        """Join alerts with outcomes for per-tier precision analysis."""
        async with self._connection.execute(
            """
            SELECT a.id, a.mint, a.symbol, a.generated_at, a.payload,
                   o.horizon_min, o.pct_change
            FROM alerts a
            JOIN alert_outcomes o ON o.alert_id = a.id
            ORDER BY a.id, o.horizon_min
            """
        ) as cursor:
            rows = await cursor.fetchall()
        summary = []
        for row in rows:
            tier = None
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
                tier = payload.get("score", {}).get("tier")
            except Exception:
                pass
            summary.append(
                {
                    "alert_id": row["id"],
                    "mint": row["mint"],
                    "symbol": row["symbol"],
                    "tier": tier,
                    "horizon_min": row["horizon_min"],
                    "pct_change": row["pct_change"],
                }
            )
        return summary
