"""EVM token safety: honeypot / sellability verification for Robinhood Chain.

Why this exists: a honeypot ("Modam"/DAM) was alerted which holders could
BUY but never SELL. It defeated every static check we had — LP was 99.99%
burned to 0x…dead and the owner was renounced, which make a token look safe
while the sell path is blocked in the contract.

The only reliable signal is BEHAVIOURAL: simulate a sell and see whether it
reverts. This module:
  1. locates the token's liquidity pair (v2 factories + v3 fee tiers),
  2. finds real holders from recent Transfer logs,
  3. simulates holder -> pair transfers (a sell) via eth_call,
  4. scans bytecode for dangerous control selectors,
  5. checks LP burn/lock and owner renouncement (necessary, NOT sufficient).

Verdicts: "sell_blocked" (never alert), "verified_sellable", "unverified".
"""

from __future__ import annotations

import logging
from typing import Any

from eth_abi import decode as eth_abi_decode
from web3 import Web3

logger = logging.getLogger(__name__)

# Robinhood Chain contracts
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
V2_FACTORIES = [
    "0x0d1ebb179cdbca88d74c923c4255cb2b17474afd",
    "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
]
V3_FACTORY = "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA"
V3_FEE_TIERS = (10000, 3000, 500)
BURN_ADDRESSES = {
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
}

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Control functions that indicate the contract can restrict trading/selling.
DANGEROUS_SELECTORS = {
    "blacklist(address)": "blacklist",
    "setBlacklist(address,bool)": "set blacklist",
    "addBotToBlackList(address)": "bot blacklist",
    "pause()": "pause",
    "unpause()": "unpause",
    "setMaxTxAmount(uint256)": "max tx limit",
    "setMaxWalletAmount(uint256)": "max wallet limit",
    "enableTrading()": "trading toggle",
    "setTrading(bool)": "trading toggle",
    "setFees(uint256,uint256)": "fee control",
    "setSellFee(uint256)": "sell fee control",
    "setBuyFee(uint256)": "buy fee control",
    "updateSellFees(uint256)": "sell fee update",
    "excludeFromFee(address)": "fee exclusion",
    "setAutomatedMarketMakerPair(address,bool)": "amm pair control",
    "setCooldown(uint256)": "cooldown",
    "setBlacklistEnabled(bool)": "blacklist toggle",
}

FUNCTION_SELECTORS = {
    "balanceOf": Web3.keccak(text="balanceOf(address)")[:4].hex(),
    "transfer": Web3.keccak(text="transfer(address,uint256)")[:4].hex(),
    "totalSupply": Web3.keccak(text="totalSupply()")[:4].hex(),
    "owner": Web3.keccak(text="owner()")[:4].hex(),
    "getPair": Web3.keccak(text="getPair(address,address)")[:4].hex(),
    "getPool": Web3.keccak(text="getPool(address,address,uint24)")[:4].hex(),
    "getReserves": Web3.keccak(text="getReserves()")[:4].hex(),
}


def _addr_topic(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def _encode_addr(address: str) -> str:
    return address[2:].lower().rjust(64, "0")


class EvmSafetyClient:
    """Behavioural safety analysis for EVM (Robinhood Chain) tokens."""

    def __init__(self, robinhood_client):
        self.rpc = robinhood_client

    # ── pair discovery ───────────────────────────────────────────────────

    async def find_pair(self, token: str) -> str | None:
        """Locate the token's liquidity pair (v2 getPair, then v3 getPool)."""
        for factory in V2_FACTORIES:
            data = (
                "0x" + FUNCTION_SELECTORS["getPair"]
                + _encode_addr(token) + _encode_addr(WETH)
            )
            res = await self.rpc._rpc("eth_call", [{"to": factory, "data": data}, "latest"])
            if res and len(res) >= 42 and int(res, 16) != 0:
                return "0x" + res[-40:]
        for fee in V3_FEE_TIERS:
            data = (
                "0x" + FUNCTION_SELECTORS["getPool"]
                + _encode_addr(token) + _encode_addr(WETH)
                + hex(fee)[2:].rjust(64, "0")
            )
            res = await self.rpc._rpc("eth_call", [{"to": V3_FACTORY, "data": data}, "latest"])
            if res and len(res) >= 42 and int(res, 16) != 0:
                return "0x" + res[-40:]
        return None

    # ── state helpers ────────────────────────────────────────────────────

    async def _balance_of(self, token: str, who: str) -> int:
        data = "0x" + FUNCTION_SELECTORS["balanceOf"] + _encode_addr(who)
        res = await self.rpc._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        try:
            return int(res, 16) if res and res != "0x" else 0
        except (TypeError, ValueError):
            return 0

    async def find_holders(self, token: str, pair: str, chunk: int = 2000, chunks: int = 6) -> list[str]:
        """Recent token recipients (real holders) sorted by balance desc."""
        latest = await self.rpc.get_block_number()
        recipients: set[str] = set()
        for i in range(chunks):
            hi = latest - i * chunk
            lo = max(0, hi - chunk)
            logs = await self.rpc.get_logs(lo, hi, [token], [[TRANSFER_TOPIC]])
            for log in logs:
                topics = log.get("topics") or []
                if len(topics) < 3:
                    continue
                to_addr = "0x" + topics[2][-40:]
                if to_addr.lower() not in BURN_ADDRESSES and to_addr.lower() != pair.lower():
                    recipients.add(to_addr)
            if len(recipients) >= 8:
                break

        holders = []
        for addr in list(recipients)[:10]:
            bal = await self._balance_of(token, addr)
            if bal > 0:
                holders.append((bal, addr))
        holders.sort(reverse=True)
        return [addr for _, addr in holders]

    async def simulate_sell(self, token: str, pair: str, holder: str, amount: int = 1) -> bool | None:
        """eth_call a holder→pair transfer. None result = revert (sell blocked)."""
        data = (
            "0x" + FUNCTION_SELECTORS["transfer"]
            + _encode_addr(pair) + hex(amount)[2:].rjust(64, "0")
        )
        res = await self.rpc._rpc("eth_call", [{"from": holder, "to": token, "data": data}, "latest"])
        if res is None:
            return False  # reverted → sell path blocked
        # transfer() returns bool true
        try:
            return int(res, 16) != 0
        except (TypeError, ValueError):
            return None

    # ── static analysis ──────────────────────────────────────────────────

    async def static_flags(self, token: str) -> list[str]:
        code = await self.rpc._rpc("eth_getCode", [token, "latest"])
        if not isinstance(code, str) or len(code) < 4:
            return []
        code_hex = code[2:].lower()
        flags = []
        for sig, label in DANGEROUS_SELECTORS.items():
            if Web3.keccak(text=sig)[:4].hex() in code_hex:
                flags.append(label)
        return sorted(set(flags))

    async def check_lp(self, pair: str) -> dict[str, Any]:
        """LP burn/lock check — necessary but NOT sufficient (honeypots burn LP)."""
        total_res = await self.rpc._rpc(
            "eth_call", [{"to": pair, "data": "0x" + FUNCTION_SELECTORS["totalSupply"]}, "latest"]
        )
        try:
            total = int(total_res, 16) if total_res else 0
        except (TypeError, ValueError):
            total = 0
        burned = 0
        for addr in BURN_ADDRESSES:
            burned += await self._balance_of(pair, addr)
        pct = (burned / total * 100) if total else 0.0
        return {"lp_total": total, "lp_burned_pct": round(pct, 2), "lp_burned": pct >= 90}

    async def check_owner_renounced(self, token: str) -> bool | None:
        data = "0x" + FUNCTION_SELECTORS["owner"]
        res = await self.rpc._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        if not res or res == "0x":
            return None
        try:
            return int(res, 16) == 0
        except (TypeError, ValueError):
            return None

    # ── main entry ───────────────────────────────────────────────────────

    async def analyze(self, token: str, pair: str | None = None) -> dict[str, Any]:
        """Full safety analysis. Returns a SafetyInfo-compatible dict."""
        result: dict[str, Any] = {
            "safety": {},
            "sources": {"evm_safety": {}},
        }
        findings: dict[str, Any] = {}

        pair = pair or await self.find_pair(token)
        findings["pair"] = pair

        flags = await self.static_flags(token)
        findings["flags"] = flags

        lp = await self.check_lp(pair) if pair else {}
        findings["lp"] = lp

        renounced = await self.check_owner_renounced(token)
        findings["owner_renounced"] = renounced

        # Behavioural sell test — the decisive check.
        sell_blocked: bool | None = None
        holders_checked = 0
        if pair:
            holders = await self.find_holders(token, pair)
            findings["holders_found"] = len(holders)
            for holder in holders[:3]:
                ok = await self.simulate_sell(token, pair, holder)
                holders_checked += 1
                if ok is False:
                    sell_blocked = True  # a real holder cannot sell
                    break
                if ok is True:
                    sell_blocked = False
        findings["holders_checked"] = holders_checked

        if sell_blocked is True:
            verdict = "sell_blocked"
        elif sell_blocked is False:
            verdict = "verified_sellable"
        else:
            verdict = "unverified"
        findings["verdict"] = verdict

        result["sources"]["evm_safety"] = findings
        result["safety"] = {
            "is_honeypot": True if sell_blocked is True else None,
            "sell_verdict": verdict,
            "lp_locked": lp.get("lp_burned"),
            "lp_locked_pct": lp.get("lp_burned_pct"),
            "mint_authority_enabled": (not renounced) if renounced is not None else None,
            "risk_flags": flags,
        }
        logger.info(
            "EVM safety %s: %s (holders checked %d, LP burned %s, flags %s)",
            token[:10], verdict, holders_checked, lp.get("lp_burned"), flags,
        )
        return result
