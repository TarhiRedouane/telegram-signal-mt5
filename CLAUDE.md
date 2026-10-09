# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Archon-first task management

**CRITICAL — overrides all other instructions/patterns:** before any task-management scenario, check if the Archon MCP server is available and use it as the primary task system. TodoWrite is only for personal secondary tracking after Archon is set up. If you used TodoWrite first, stop and restart with Archon.

Standard cycle: `archon:manage_task(action="get", ...)` → research via `archon:search_code_examples()` / `archon:perform_rag_query()` → implement → `archon:manage_task(action="update", update_fields={"status": "review"})` → get next `todo` task. Never skip the status update; mark tasks `review` (not `done`) when implementation is complete so the user can confirm/test.

## Project overview

Python 3.12 async automation: Telegram → signal parsing (regex fast-path, LLM fallback) → correlation engine (reply-chain + time-window) → risk manager (break-even/close/modify) → MT5 executor. Monitors GOLD/XAUUSD in French. Entry points: `main.py` (async event loop) and `cli.py` (start/stop/status).

Design docs are the source of truth:
- `docs/architecture/high-level-architecture.md`, `components.md`, `core-workflows.md`, `execution-sequence-method-level.md`
- `docs/architecture/coding-standards.md`, `database-schema.md`, `error-handling-strategy.md`
- `docs/prd/*` for features

## Modules

**src/telegram_client/** — Telethon-based Telegram integration
- `client.py` — TelegramClient async wrapper
- `auth_manager.py` — phone login, session storage
- `message_handler.py` — receive & queue Telegram messages
- `rate_limiter.py` — human delay emulation (anti-ban)
- `session_manager.py` — .session file lifecycle
- `errors.py` — Telegram-specific exceptions

**src/signal_parser/** — Regex-first hybrid parsing
- `regex_parser.py` — fast-path pattern matching (ENTRY/BREAK EVEN/CLOSE)
- `llm_parser.py` — GPT-4o fallback (unparseable signals)
- `context_analyzer.py` — extract symbol, price, direction from raw text
- `patterns.py` — regex definitions (French/English templates)
- `symbol_policy.py` — symbol whitelist & normalization

**src/correlation_engine/** — Link follow-ups to original signals
- `correlator.py` — orchestrates reply_tracer + time_matcher
- `reply_tracer.py` — walk reply chain (Telegram native)
- `time_matcher.py` — fallback: find signal by time window + symbol

**src/risk_manager/** — Trade lifecycle (BE/close/modify)
- `break_even.py` — move SL to entry
- `close_processor.py` — close position, remove from tracking
- `position_modifier.py` — adjust TP/SL
- `context_processor.py` — enrich signal with market context

**src/mt5_executor/** — MetaTrader 5 integration
- `connection.py` — MT5ConnectionManager (login, pool, health)
- `position_manager.py` — open/close/modify orders
- `symbol_specs.py` — fetch pip size, contract size, margin

**src/database/** — SQLAlchemy + aiosqlite
- `repository.py` — repository pattern (signals, positions, trades)
- `models.py` — SQLAlchemy ORM (Signal, Position, Trade, BreakEven)
- `cache.py` — LLM response cache (avoid duplicate API calls)
- `migrations/` — Alembic scripts

**src/monitoring/** — Health & observability
- `health_checker.py` — circuit breaker status, connectivity checks
- `metrics_collector.py` — trade count, error rate, latency
- `console_dashboard.py` — real-time TUI (Telegram updates, positions)
- `alert_manager.py` — Telegram alerts on critical events

**src/utils/**
- `circuit_breaker.py` — decorator for OpenAI/MT5 calls
- `currency_config.py` — pip sizes, lot sizes, margin by symbol

## Commands

```bash
# Run
python main.py                          # async event loop: monitor → parse → correlate → execute
python cli.py start|stop|status         # lifecycle mgmt (fork subprocess, track PID)

# Setup (one-time)
python scripts/migrate_database.py      # Alembic: create schema
python scripts/benchmark_db.py          # perf test

# Tests
pytest                                  # full suite (~700 tests)
pytest tests/unit/                      # unit only (~630 tests, ~1 min)
pytest tests/integration/               # integration only (~70 tests)
pytest tests/unit/test_correlator.py::test_name  # single test
pytest --cov=src                        # coverage report

# Lint/format/type-check (run all three before commit)
black src/ tests/
ruff check src/ tests/
mypy src/
```

pytest configured: `--strict-markers --strict-config -W error`. Markers: `unit`, `integration`, `slow`, `external`.

## Architecture

Event-driven async pipeline (asyncio, no threads). Data flow:

```
┌─ Telegram groups (MTProto)
│    ↓ MessageHandler queues raw messages
│    ↓ RegexParser fast-path → 90% signals parsed instantly
│    ├─ (unparseable → LLMParser:GPT-4o, circuit_breaker protected)
│    ↓ ContextAnalyzer normalizes: symbol, price, direction, type
│    ↓ CorrelationEngine links replies to originals
│    │  ├─ reply_tracer: Telegram native reply chain
│    │  └─ time_matcher: fallback by timestamp + symbol
│    ↓ RiskManager: actions (BE/CLOSE/MODIFY)
│    │  ├─ BreakEven: update SL
│    │  ├─ CloseProcessor: liquidate
│    │  └─ PositionModifier: TP/SL adjustments
│    ↓ MT5Executor: live orders
│    │  └─ circuit_breaker on all calls
│    ↓ Repository: Signal/Position/Trade records
│    ↓ MetaTrader 5 terminal
│
├─ Database: SQLAlchemy + aiosqlite
│  └─ cache.py: LLM dedup (avoid repeated OpenAI calls)
│
├─ Monitoring (async tasks, parallel to main loop)
│  ├─ HealthChecker: circuit breaker state
│  ├─ MetricsCollector: error rate, latency, trade count
│  └─ ConsoleDashboard: live TUI
│
└─ AlertManager: critical events → Telegram DMs
```

**Key rules** (see `docs/architecture/coding-standards.md`):
- **Repository pattern only** — `src/database/repository.py` is the sole DB access point.
- **Circuit breaker required** on OpenAI + MT5 calls — `src/utils/circuit_breaker.py` decorator.
- **Regex first** — LLM is fallback only. Cost control.
- **Correlation priority** — reply-chain lookup before time-window fallback.
- **Telegram rate limit** — all calls via `rate_limiter.human_delay()` (anti-ban).
- **Price normalization** — `normalize_price()` to pip size before MT5.
- **High-risk code** — Telegram + MT5 integration: narrow, observable changes only. No broad refactors.
- **Secrets** — never log API keys; hash usernames. Never commit `.env`, `.session`.

**Recent work** (commits: `correlator to test later`, `working order execution`, `fix duplicate signals and open order mt5`):
- Position tracking: open order detection, duplicate signal filtering
- Correlator refactor: improved reply-chain traversal
- Pending: comprehensive correlator testing

**Config:** `config/settings.py` (pydantic), `logging_config.py`, `openai_config.py`, `prompts/*.txt`. Logs: `logs/app.log`, `logs/error.log`, `logs/trades.log`.
