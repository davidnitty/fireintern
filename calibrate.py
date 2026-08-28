"""Threshold calibration report from the backtesting ledger.

Reads the alerts + alert_outcomes tables and prints per-tier precision at
each horizon, e.g. "what share of DIAMOND alerts were up >= 20% after 1h".

Usage:
    python calibrate.py [--target 20] [--db memecoin_alert_bot.db]

Numbers are measured from YOUR runtime data — use them to tune
MIN_CONFIDENCE / tier thresholds with evidence, not intuition (guide §5).
"""

from __future__ import annotations

import argparse
import asyncio

from memecoin_alert_bot.storage.sqlite import Storage

HORIZONS = [5, 15, 60, 1440]


def label(horizon: int) -> str:
    return {5: "5m", 15: "15m", 60: "1h", 1440: "24h"}.get(horizon, f"{horizon}m")


async def main_async(target_pct: float, db_path: str) -> None:
    storage = Storage(db_path)
    await storage.connect()
    try:
        rows = await storage.calibration_summary()
    finally:
        await storage.close()

    if not rows:
        print("No outcomes recorded yet. Let the bot run for at least a few")
        print("hours — outcomes appear at +5m/+15m/+1h/+24h after each alert.")
        return

    tiers = sorted({r["tier"] or "?" for r in rows})
    print(f"\nCalibration report (target: >= +{target_pct:.0f}% price move)\n")
    print(f"{'tier':<12} {'horizon':>7} {'alerts':>7} {'hits':>6} {'precision':>10}")
    print("-" * 48)

    for tier in tiers:
        for horizon in HORIZONS:
            group = [
                r for r in rows
                if r["tier"] == tier and r["horizon_min"] == horizon
                and r["pct_change"] is not None
            ]
            if not group:
                continue
            hits = sum(1 for r in group if (r["pct_change"] or 0) >= target_pct)
            precision = hits / len(group) * 100
            print(f"{tier:<12} {label(horizon):>7} {len(group):>7} {hits:>6} {precision:>9.1f}%")
    print()

    # Overall coverage: how many alerts produced outcomes.
    total_alert_rows = len({r["alert_id"] for r in rows})
    print(f"Alerts with outcomes: {total_alert_rows}")
    print("Raise/lower MIN_CONFIDENCE and tier thresholds based on these")
    print("measured precision numbers, not intuition.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibration report")
    parser.add_argument("--target", type=float, default=20.0, help="Success threshold %% move (default 20)")
    parser.add_argument("--db", type=str, default="memecoin_alert_bot.db", help="SQLite DB path")
    args = parser.parse_args()
    asyncio.run(main_async(args.target, args.db))


if __name__ == "__main__":
    main()
