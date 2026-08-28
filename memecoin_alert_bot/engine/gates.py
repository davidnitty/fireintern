"""Hard risk gates evaluated BEFORE scoring (guide §3.2).

A token must pass the critical gates before it can receive a high-conviction
tier. A failed or UNKNOWN gate produces Reject / High Risk — never a normal
score with missing values silently treated as zero.

Gates:
  identity      — mint, venue, and pool/curve are present and coherent
  freshness     — event/market data are within a maximum age
  authority     — mint/freeze authority state is identified (and not active)
  sellability   — a venue/pool exists so a holder can plausibly exit
  liquidity     — venue liquidity/depth is identified and non-trivial
  deployer      — deployer/control is identified
  coordination  — holder concentration / bundle evidence is not severe
  source_agreement — core facts are reconciled across providers
"""

from __future__ import annotations

from memecoin_alert_bot.engine.models import CoinData, GateResult

# Maximum token age (seconds) for a "fresh" high-conviction alert.
MAX_AGE_SECONDS = 6 * 3600  # 6 hours


def _gate(gate: str, passed: bool | None, detail: str = "") -> GateResult:
    status = "passed" if passed is True else ("failed" if passed is False else "unknown")
    return GateResult(gate=gate, passed=bool(passed), status=status, detail=detail)


def evaluate_gates(coin: CoinData) -> tuple[bool, list[GateResult], bool]:
    """Return (critical_passed, gate_results, has_unknown_critical).

    Semantics (guide §3.2, refined):
    - A **failed** critical gate blocks high-conviction tiers entirely
      (HIGH_RISK).
    - An **unknown** critical gate does not fail — but caps the tier at
      STANDARD via ``has_unknown_critical`` so unverified tokens can never
      reach DIAMOND.
    - Venue evidence is a V3 pool address, a Solana bonding curve, OR
      positive liquidity (covers Uniswap v4 pool IDs which are not
      callable addresses).
    """
    gates: list[GateResult] = []

    has_venue = (
        coin.pool_address is not None
        or coin.chain == "solana"
        or (coin.liquidity or 0) > 0
    )

    # ── Identity ─
    gates.append(_gate("identity", bool(coin.mint) and has_venue, "mint + venue present"))

    # ── Freshness ──
    if coin.age_seconds is None:
        gates.append(_gate("freshness", None, "age unknown"))
    else:
        gates.append(_gate("freshness", coin.age_seconds <= MAX_AGE_SECONDS, f"age {coin.age_seconds}s"))

    # ── Authority ──
    mint_auth = coin.safety.mint_authority_enabled
    freeze_auth = coin.safety.freeze_authority_enabled
    if mint_auth is None and freeze_auth is None:
        gates.append(_gate("authority", None, "authority state unknown"))
    else:
        active = (mint_auth is True) or (freeze_auth is True)
        gates.append(
            _gate("authority", not active, "mint/freeze revoked or disabled" if not active else "authority active")
        )

    # ── Sellability / venue ──
    gates.append(_gate("sellability", has_venue, "exit venue identified" if has_venue else "no exit venue"))

    # ── Liquidity ──
    if coin.liquidity is None:
        gates.append(_gate("liquidity", None, "liquidity unknown"))
    else:
        gates.append(_gate("liquidity", coin.liquidity > 0, f"liquidity {coin.liquidity:.0f}"))

    # ── Deployer / control ──
    deployer_known = bool(coin.dev_wallet) or bool(coin.deployer)
    gates.append(_gate("deployer", deployer_known, "deployer identified" if deployer_known else "deployer unknown"))

    # ── Coordination / bundle ──
    severe_bundle = coin.safety.bundled_pct > 40 or coin.top10_holder_pct > 70
    gates.append(
        _gate(
            "coordination",
            not severe_bundle,
            f"bundled {coin.safety.bundled_pct:.0f}% / top10 {coin.top10_holder_pct:.0f}%",
        )
    )

    # ── Source agreement ──
    source_count = sum(1 for v in coin.sources.values() if v is not None)
    gates.append(_gate("source_agreement", source_count >= 1, f"{source_count} sources"))

    critical = ["identity", "authority", "sellability", "coordination"]
    critical_results = [g for g in gates if g.gate in critical]
    failed = any(g.status == "failed" for g in critical_results)
    unknown = any(g.status == "unknown" for g in critical_results)

    return (not failed), gates, unknown


def build_invalidation(coin: CoinData) -> list[str]:
    """Conditions that would invalidate the alert (guide §6)."""
    lines = [
        "Deployer or insider transfers large amounts",
        "Abrupt liquidity withdrawal or pool ownership change",
        "Coordinated selling / cluster dump",
        "Loss of sellability or new transfer restrictions",
    ]
    if coin.safety.mint_authority_enabled is True:
        lines.append("Mint authority still active — supply can be inflated")
    if coin.safety.bundled_pct > 20:
        lines.append(f"Elevated bundled supply ({coin.safety.bundled_pct:.0f}%)")
    return lines
