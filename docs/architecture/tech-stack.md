# Tech Stack

## Infrastructure

- **Runs on:** local Windows PC (MetaTrader 5 terminals are Windows apps); VPS-ready later
- **Process model:** one bot process per broker instance, started by `cli.py`; each attaches to its own
  portable MT5 terminal. The MetaTrader5 Python API allows one terminal per process.

## Technology Stack Table

Versions from `requirements.txt`.

| Category | Technology | Version | Used for |
|----------|-----------|---------|----------|
| **Language** | Python | 3.12 | Everything |
| **Async Runtime** | asyncio | Built-in | Telegram events, price polling, watchers |
| **Telegram Client** | Telethon | 1.40.0 | User session, channel messages and edits |
| **Trading Platform** | MetaTrader5 | 5.0.5200 | Orders, positions, ticks, candles, account/terminal info |
| **Database** | SQLite + aiosqlite | 0.20.0 | Signals, positions, LLM cache (one DB per instance) |
| **ORM** | SQLAlchemy | 2.0.43 | Async ORM behind the repository |
| **Migrations** | alembic | 1.15.2 | Schema migrations |
| **LLM** | openai | 1.99.1 | gpt-5-mini fallback parsing and context analysis |
| **Configuration** | python-dotenv, pydantic, pydantic-settings | 1.0.1 / 2.10.6 / 2.6.1 | `.env` loading, MT5 settings validation |
| **Broker profiles** | JSON (`config/brokers.json`) | — | One MT5 account/terminal per instance |
| **JSON** | orjson | 3.10.12 | LLM response parsing |
| **Console UI** | rich | 13.9.4 | Live dashboard per instance |
| **System metrics / processes** | psutil | 6.1.0 | Dashboard metrics, `cli.py` process checks |
| **Logging** | Python logging | Built-in | JSON logs, rotating files per instance |
| **Testing** | pytest, pytest-asyncio, pytest-mock | 8.4.1 / 0.25.0 / 3.14.0 | Unit and integration tests |
| **Quality** | black, ruff, mypy | 24.10.0 / 0.8.4 / 1.13.0 | Format, lint, types |

Installed but not used by the code today: APScheduler, pendulum, aiohttp, aiofiles (candidates for
removal, or for the control page in roadmap step 16).
