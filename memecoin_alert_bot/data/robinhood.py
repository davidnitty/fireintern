"""Robinhood Chain (Arbitrum Orbit L2) RPC client for token launch monitoring."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from eth_abi import decode as eth_abi_decode
from eth_abi import encode as eth_abi_encode
from eth_utils import to_checksum_address
from web3 import Web3

from memecoin_alert_bot.utils.helpers import fetch_json

logger = logging.getLogger(__name__)

DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
CHAIN_ID = 4663
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
SWAP_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbca67"
ETH_USD = 3500.0  # rough ETH/USD oracle for market-cap estimates
BLOCK_TIME_SECONDS = 0.25  # Robinhood is an Arbitrum Orbit chain


def estimate_market_cap(price_in_pair: float | None, total_supply: float | None, eth_usd: float = ETH_USD) -> float | None:
    """Estimate USD market cap from token price (in WETH) and total supply."""
    if price_in_pair is None or total_supply is None or price_in_pair <= 0 or total_supply <= 0:
        return None
    return price_in_pair * total_supply * eth_usd

# Minimal ABI fragments used for eth_call.
FUNCTION_SELECTORS = {
    "name": Web3.keccak(text="name()")[:4].hex(),
    "symbol": Web3.keccak(text="symbol()")[:4].hex(),
    "decimals": Web3.keccak(text="decimals()")[:4].hex(),
    "totalSupply": Web3.keccak(text="totalSupply()")[:4].hex(),
    "description": Web3.keccak(text="description()")[:4].hex(),
    "socials": Web3.keccak(text="socials()")[:4].hex(),
    "liquidityPool": Web3.keccak(text="liquidityPool()")[:4].hex(),
    "slot0": Web3.keccak(text="slot0()")[:4].hex(),
    "token0": Web3.keccak(text="token0()")[:4].hex(),
    "token1": Web3.keccak(text="token1()")[:4].hex(),
}


class RobinhoodChainClient:
    """Async JSON-RPC client for Robinhood Chain."""

    def __init__(
        self,
        rpc_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ):
        self.rpc_url = rpc_url or DEFAULT_RPC
        self._owned_session = session is None
        self._session_ref = session
        self._session: aiohttp.ClientSession | None = None
        self._call_pace = asyncio.Semaphore(50)  # be polite to public RPC

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = self._session_ref or aiohttp.ClientSession()
            self._session_ref = None
            self._owned_session = True
        return self._session

    async def close(self) -> None:
        if self._owned_session and self._session and not self._session.closed:
            await self._session.close()

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        async with self._call_pace:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }
            data = await fetch_json(
                self.session,
                self.rpc_url,
                method="POST",
                payload=payload,
                timeout=25,
                retries=3,
            )
            if data is None:
                return None
            if "error" in data:
                logger.debug("RPC error: %s", data["error"])
                return None
            return data.get("result")

    async def get_block_number(self) -> int:
        """Return the latest block number."""
        result = await self._rpc("eth_blockNumber", [])
        return int(result, 16) if result else 0

    async def get_logs(
        self,
        from_block: int,
        to_block: int,
        addresses: list[str],
        topics: list[str | list[str] | None],
    ) -> list[dict[str, Any]]:
        """Fetch event logs for a block range."""
        params = [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": [to_checksum_address(a) for a in addresses],
                "topics": topics,
            }
        ]
        result = await self._rpc("eth_getLogs", params)
        return result if isinstance(result, list) else []

    async def batch_call(self, calls: list[dict[str, Any]]) -> list[Any]:
        """Execute a JSON-RPC batch of eth_call requests."""
        if not calls:
            return []
        payload = [
            {
                "jsonrpc": "2.0",
                "id": i + 1,
                "method": "eth_call",
                "params": [call, "latest"],
            }
            for i, call in enumerate(calls)
        ]
        async with self._call_pace:
            data = await fetch_json(
                self.session, self.rpc_url, method="POST", payload=payload, timeout=30, retries=3
            )
        if not isinstance(data, list):
            return []
        results = [None] * len(calls)
        for item in data:
            idx = item.get("id", 1) - 1
            if 0 <= idx < len(results):
                if "error" in item:
                    logger.debug("eth_call error: %s", item["error"])
                else:
                    results[idx] = item.get("result")
        return results

    @staticmethod
    def selector(name: str) -> str:
        return FUNCTION_SELECTORS[name]

    @staticmethod
    def decode_string(hex_value: str) -> str:
        """Decode ABI-encoded string or return empty."""
        if not hex_value or hex_value == "0x":
            return ""
        try:
            raw = bytes.fromhex(hex_value.replace("0x", ""))
            decoded = eth_abi_decode(["string"], raw)
            return decoded[0]
        except Exception:
            return ""

    @staticmethod
    def decode_uint(hex_value: str) -> int:
        if not hex_value:
            return 0
        try:
            return int(hex_value, 16)
        except ValueError:
            return 0

    @staticmethod
    def decode_address(hex_value: str) -> str:
        if not hex_value or hex_value == "0x":
            return ""
        try:
            return "0x" + hex_value[-40:]
        except Exception:
            return ""

    @staticmethod
    def decode_sqrt_price_x96(sqrt_price_x96: int) -> float:
        """Convert Uniswap V3 sqrtPriceX96 to a float ratio."""
        if sqrt_price_x96 == 0:
            return 0.0
        ratio = sqrt_price_x96 / (2**96)
        return ratio * ratio

    async def fetch_token_metadata(self, token: str) -> dict[str, Any]:
        """Fetch Pons-style token metadata via batch eth_call."""
        token = to_checksum_address(token)
        calls = [
            {"to": token, "data": "0x" + self.selector("name")},
            {"to": token, "data": "0x" + self.selector("symbol")},
            {"to": token, "data": "0x" + self.selector("decimals")},
            {"to": token, "data": "0x" + self.selector("totalSupply")},
            {"to": token, "data": "0x" + self.selector("description")},
            {"to": token, "data": "0x" + self.selector("liquidityPool")},
            {"to": token, "data": "0x" + self.selector("socials")},
            # EVM ownership facts (generic ERC-20 / Ownable).
            {"to": token, "data": "0x" + self.selector("owner")},
        ]
        results = await self.batch_call(calls)
        name = self.decode_string(results[0]) if results[0] else ""
        symbol = self.decode_string(results[1]) if results[1] else "UNKNOWN"
        decimals = self.decode_uint(results[2]) if results[2] else 18
        total_supply_raw = self.decode_uint(results[3]) if results[3] else 0
        description = self.decode_string(results[4]) if results[4] else ""
        pool = self.decode_address(results[5]) if results[5] else ""
        socials_raw = []
        if results[6]:
            raw = bytes.fromhex(results[6].replace("0x", ""))
            try:
                socials_raw = list(eth_abi_decode(["string", "string", "string", "string", "string"], raw))
            except Exception:
                pass

        total_supply = total_supply_raw / (10**decimals) if decimals else total_supply_raw

        owner = self.decode_address(results[7]) if len(results) > 7 and results[7] else ""
        ownership_renounced = owner.lower() in ("", "0x", "0x" + "0" * 40, f"0x{'0' * 40}")

        return {
            "name": name,
            "symbol": symbol,
            "decimals": decimals,
            "total_supply": total_supply,
            "description": description,
            "pool_address": pool,
            "owner": owner,
            "ownership_renounced": ownership_renounced,
            "socials": {
                k: v
                for k, v in zip(
                    ["twitter", "telegram", "discord", "website", "farcaster"],
                    socials_raw,
                )
                if v
            },
        }

    async def fetch_pool_price(self, pool: str, token: str, pair_token: str | None = None) -> dict[str, Any]:
        """Fetch slot0 from a Uniswap V3 pool and compute token price."""
        if not pool:
            return {"price": None, "liquidity": None}
        pool = to_checksum_address(pool)
        result = await self._rpc("eth_call", [{"to": pool, "data": "0x" + self.selector("slot0")}, "latest"])
        if not result:
            return {"price": None, "liquidity": None}
        try:
            raw = bytes.fromhex(result.replace("0x", ""))
            sqrt_price_x96, tick, *_ = eth_abi_decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], raw)
            ratio = self.decode_sqrt_price_x96(sqrt_price_x96)

            pair = to_checksum_address(pair_token) if pair_token else to_checksum_address(WETH)
            # Query actual pool ordering; lexical address comparison is unsafe.
            token0_raw = await self._rpc("eth_call", [{"to": pool, "data": "0x" + self.selector("token0")}, "latest"])
            token0 = self.decode_address(token0_raw) if token0_raw else ""
            is_token0 = token.lower() == token0.lower()
            price_in_pair = ratio if is_token0 else 1 / ratio if ratio else 0.0
            # We don't have an ETH/USD oracle here; return nominal price.
            return {
                "price": price_in_pair,
                "liquidity": None,  # Could call liquidity() if needed
                "is_token0": is_token0,
            }
        except Exception as exc:
            logger.debug("Pool price decode error: %s", exc)
            return {"price": None, "liquidity": None}

    async def fetch_recent_swaps(
        self,
        pool: str,
        token: str,
        pair_token: str | None = None,
        blocks_back: int = 10_000,
    ) -> dict[str, Any]:
        """Query recent Uniswap V3 Swap events to compute buy/sell volume and pressure.

        Returns a dict with buy_volume, sell_volume, buy_pressure (0-1),
        and total volume in pair-token terms.
        """
        if not pool:
            return {"buy_volume": 0.0, "sell_volume": 0.0, "buy_pressure": 0.5, "volume": 0.0}

        pool = to_checksum_address(pool)
        try:
            latest = int((await self._rpc("eth_blockNumber", [])) or "0x0", 16)
        except Exception:
            return {"buy_volume": 0.0, "sell_volume": 0.0, "buy_pressure": 0.5, "volume": 0.0, "window_seconds": 0, "unit": "unknown"}

        from_block = max(0, latest - blocks_back)
        # Use actual V3 pool token order, never lexical address order.
        token0_raw = await self._rpc("eth_call", [{"to": pool, "data": "0x" + self.selector("token0")}, "latest"])
        token0 = self.decode_address(token0_raw) if token0_raw else ""
        is_token0 = token.lower() == token0.lower()

        logs = await self.get_logs(from_block, latest, [pool], [[SWAP_TOPIC0]])
        buy_vol = 0.0
        sell_vol = 0.0
        buys = 0
        sells = 0
        for log in logs:
            try:
                raw = bytes.fromhex(log.get("data", "0x").replace("0x", ""))
                amount0, amount1 = eth_abi_decode(["int256", "int256"], raw[:64])
                token_signed = int(amount0) if is_token0 else int(amount1)
                pair_signed = int(amount1) if is_token0 else int(amount0)
            except Exception:
                continue

            # Uniswap V3 deltas are from the pool's perspective:
            # tracked token < 0 => pool sent token => user BUY
            # tracked token > 0 => pool received token => user SELL
            # Pair amount is chain-native / pair-denominated; keep it directional
            # only, never map it into USD 1h/24h fields.
            swap_size = abs(pair_signed) / 1e18
            if token_signed < 0:
                buy_vol += swap_size
                buys += 1
            elif token_signed > 0:
                sell_vol += swap_size
                sells += 1

        total_vol = buy_vol + sell_vol
        pressure = buy_vol / total_vol if total_vol > 0 else 0.5
        return {
            "buy_volume": buy_vol,
            "sell_volume": sell_vol,
            "buy_pressure": pressure,
            "volume": total_vol,
            "buys": buys,
            "sells": sells,
            "window_seconds": blocks_back * BLOCK_TIME_SECONDS,
            "unit": "pair_token_native",
        }
