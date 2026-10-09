# Coding Standards

## Core Standards

- **Languages & Runtimes:** Python 3.12, asyncio for all I/O operations
- **Style & Linting:** black + ruff, line length 100 (`pyproject.toml`, `.ruff.toml`); mypy for `src/`
- **Test Organization:** `tests/unit/test_{module}.py`, `tests/integration/test_{feature}_flow.py`
- **Run tests with the project venv:** `.venv/Scripts/python.exe -m pytest tests/unit/<file>`
  (pytest runs with `--strict-markers --strict-config -W error`; mark tests `unit`/`integration`)

## Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Classes | PascalCase | `PendingSetup`, `RegexParser` |
| Functions | snake_case | `parse_signal()`, `leg_volume()` |
| Constants | UPPER_SNAKE | `MAX_RETRIES`, `SUPPORTED_CLOSE_TIMEFRAMES` |
| Private methods | Leading underscore | `_validate_live_price()` |
| Settings / env vars | snake_case attr ↔ UPPER_SNAKE env | `max_open_legs` ↔ `MAX_OPEN_LEGS` |

## Critical Rules

### Safety (trading)
- **Every order goes through the risk guard:** `ApplicationManager._execute_signal_on_mt5` calls
  `risk_guard.refusal()` before `open_market_order`. Never open an order on another path.
- **Never trade without an explicit direction word:** an LLM BUY/SELL without buy/sell/achat/vente…
  in the text is discarded (`has_action_word`).
- **Complete single-message signals must never break:** `sell gold 4149 / SL / TP` executes at once;
  multi-message setups aggregate first. Add a regression test before changing parsing/routing.
- **MT5 account safety:** the bot only attaches to the terminal at `MT5_PATH`/profile path, never calls
  `mt5.login()` over another account, and refuses a demo/real mismatch (`MT5_ACCOUNT_MODE`).
- **Telegram + MT5 code is high risk:** narrow, observable changes, log every decision; test on a
  demo account before live.

### Architecture
- **Asset-specific values only in `symbol_policy.py`** (tolerances, buffers, distances, aliases).
  Engines (setup aggregator, rejection detector, risk guard) never hard-code an asset.
- **Regex first, LLM fallback only** (cost control); LLM output is validated like regex output.
- **Correlation priority:** reply chain before time window.
- **Database only through the repository pattern** (`src/database/repository.py`).
- **Circuit breaker on all OpenAI and MT5 calls** (`@circuit_breaker`).
- **Pure logic stays I/O-free** (e.g. `rejection_detector.py`, `risk_guard.refusal`, `position_sizer`)
  so it is unit-testable without MT5.
- **Per-instance runtime paths come from `settings`** (`settings.data_dir`, `settings.log_dir`,
  `get_database_url()`); never hard-code `data/` or `logs/`.

### Hygiene
- **Use the logger, not print()** — except `cli.py`, which prints user-facing CLI output.
- **Never log secrets:** no API keys or passwords; hash Telegram usernames. Broker passwords live only
  in `.env` (`BROKER_<KEY>_PASSWORD`), never in `config/brokers.json`.
- **Never commit** `.env`, `*.session`, `config/brokers.json`, `data/`, `logs/`.
- **All prices normalized to broker precision** before MT5 calls (`normalize_price()`).
- **Telegram calls respect rate limiting** (`rate_limiter.human_delay()`).
- **Async functions handle cancellation:** clean up in `try/finally`.
- **Diagnose from `logs/<broker>/app.log` and the MT5 journal**, not with live Telegram API calls.
