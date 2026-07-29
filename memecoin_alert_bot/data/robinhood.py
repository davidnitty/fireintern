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
                self.session, self.rpc_url, method="POST", payload=payload, timeout=30
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

        return {
            "name": name,
            "symbol": symbol,
            "decimals": decimals,
            "total_supply": total_supply,
            "description": description,
            "pool_address": pool,
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
            # isToken0 determines price direction.
            is_token0 = token.lower() < pair.lower()
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
