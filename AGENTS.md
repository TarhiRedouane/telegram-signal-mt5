# AGENTS.md

## Project overview

This repository is a Python 3.12 trading automation system for monitoring Telegram signal channels, validating signal intent, correlating messages, and executing trades through MetaTrader 5 (MT5). The primary runtime entry points are [main.py](main.py) and [cli.py](cli.py), with the application logic organized under [src](src).

## Architecture and documentation

Use the existing project docs as the source of truth before making design changes:

- [README.md](README.md)
- [docs/prd/index.md](docs/prd/index.md)
- [docs/architecture/index.md](docs/architecture/index.md)
- [docs/architecture/coding-standards.md](docs/architecture/coding-standards.md)
- [docs/architecture/test-strategy-and-standards.md](docs/architecture/test-strategy-and-standards.md)

The system is organized around these core responsibilities:

- Telegram integration and messaging lifecycle
- signal parsing and validation
- correlation and message-trace analysis
- risk management and position handling
- MT5 execution and monitoring
- persistence and health metrics

## Repo conventions

- Follow the existing `src` package layout and keep domain-specific logic inside the relevant module.
- Prefer explicit, typed Python functions and preserve the project's configuration patterns in [config](config).
- Keep environment and credential handling in `.env` or the existing config modules; do not commit secrets.
- For new features, check the relevant design docs first, then implement in the matching subsystem.
- Maintain test coverage for behavior changes, especially around parsing, risk logic, and MT5 interactions.
- Treat MT5 and Telegram integration code as high-risk external boundaries; prefer narrow, observable changes.

## Validation workflow

Use the project’s standard checks before completing work:

```bash
pytest
pytest tests/unit/
pytest tests/integration/
ruff check src tests
black src tests
mypy src
```

When implementing a bug fix or feature, add or update a focused failing test before the fix when practical, then verify the targeted behavior with pytest.

## Useful commands

```bash
python main.py
python cli.py start
python cli.py status
python cli.py stop
python scripts/migrate_database.py
```

## Guardrails for AI agents

- Do not duplicate existing architecture documentation; link to it instead of re-describing it.
- Keep changes scoped and aligned to the existing subsystem boundaries.
- Prefer targeted updates over broad rewrites.
- If a task touches trading or execution logic, validate the business risk and add coverage where possible.
- Preserve consistency with the project’s naming, configuration, and testing conventions from the docs above.
