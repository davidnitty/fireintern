# Memecoin Alert Bot

A Telegram bot that monitors [pump.fun](https://pump.fun) in real time and sends alerts for viral memes, celebrity coins, AI agent tokens, and scam risks using the SPYZER memecoin trading framework.

## Features

- **7 Signal Detectors**
  - 🤖 AI Agent Token
  - ⭐ Celebrity Coin
  - 🐵 Viral Meme
  - 🚀 Team-Backed
  - 🤝 Community Takeover (CTO)
  - 🚨 Bundling Risk
  - 🧛 Vamp Risk
- **SPYZER Composite Scoring**
  - Bundling risk, dev wallet safety, narrative strength, liquidity/TVL, market conditions, holder distribution, chart structure.
  - Verdicts: `BUY`, `WAIT`, `DYOR`, `PASS`.
- **Real-Time Data Sources**
  - PumpPortal WebSocket (primary)
  - pump.fun REST API (fallback)
  - DexScreener (volume/liquidity)
  - Rugcheck (safety report)
  - Solscan Pro API (holders/metadata)
  - X API v2 stub (narrative/viral detection when key provided)
  - Robinhood Chain L2 (Arbitrum Orbit, Chain ID 4663):
    - Pons launchpad (`TokenLaunched` events)
    - Noxa launchpad (polls on-chain `allTokensLength()` / `allTokens(index)` registry)
- **Telegram Delivery**
  - Rich Markdown alert cards with inline buttons.
  - Per-coin cooldown to avoid spam.
  - `/start`, `/status`, `/recent` commands.
- **Persistence**
  - SQLite storage for coin history, alerts, and backtesting snapshots.

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/davidnitty/fireintern.git
cd fireintern
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# edit .env and add:
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...
# Optional API keys:
# SOLSCAN_API_KEY=...
# RUGCHECK_API_KEY=...
# X_BEARER_TOKEN=...
```

### 3. Run

```bash
python main.py
# or
python -m memecoin_alert_bot
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHAT_ID` | Yes | Default destination chat/channel ID. |
| `SOLSCAN_API_KEY` | No | Solscan Pro API key (holders + metadata). |
| `RUGCHECK_API_KEY` | No | Rugcheck API key. Free tier works without. |
| `X_BEARER_TOKEN` | No | X API v2 bearer token for viral detection. |
| `WEBHOOK_URL` | No | Set to enable webhook mode. |
| `PORT` | No | Port for webhook server (default `8000`). |
| `ALERT_COOLDOWN_SECONDS` | No | Seconds between repeated alerts for the same coin (default `300`). |
| `SUBSCRIPTION_MODE` | No | `all` or `high` (default `all`). |
| `MIN_CONFIDENCE` | No | Minimum confidence to send alert (default `0.2`). |
| `ENABLE_PONS_ROBINHOOD` | No | Enable Robinhood Chain Pons indexer (default `true`). |
| `ENABLE_NOXA_ROBINHOOD` | No | Enable Robinhood Chain Noxa indexer (default `true`). |
| `ROBINHOOD_RPC_URL` | No | JSON-RPC endpoint for Robinhood Chain (default public RPC). |

## Architecture

```text
PumpPortal WS ──┐
pump.fun REST  ──┼──┐
DexScreener    ──┤  ├─> CoinData Normalizer ──> 7 Detectors ──> SPYZER Scorer ──> Telegram
Rugcheck       ──┤  │                                   |
Solscan        ──┘  │                                   v
                    │                           SQLite Storage
Robinhood Chain ────┘  (Pons events + Noxa registry via RPC)
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

## Roadmap

- [x] Robinhood Chain + Pons launchpad integration
- [x] Noxa (`fun.noxa.fi/rh`) launchpad integration
- [ ] Codex / Bitquery integration as stable primary data source
- [ ] Bubblemaps bundle cluster analysis
- [ ] NLP-driven narrative detection
- [ ] Historical backtesting dashboard

## Disclaimer

**Not financial advice.** This bot is an educational/research tool. Always DYOR and trade responsibly.
