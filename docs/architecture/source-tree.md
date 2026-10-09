# Source Tree

Actual layout (updated 2026-10-09). Runtime data and logs are per broker instance — see the end.

```plaintext
telegram-signal-mt5/
├── main.py                         # Async app: Telegram → parse → setups/zones → risk guard → MT5
├── cli.py                          # start/stop/status; broker menu, one process per broker
├── .env / .env.example             # Secrets + parameters (.env never committed)
├── pyproject.toml, .ruff.toml, mypy.ini, pytest.ini, requirements.txt
│
├── config/
│   ├── settings.py                 # All settings from .env; BOT_INSTANCE → broker profile + per-instance paths
│   ├── brokers.py                  # Broker profiles: load/validate, menu selection, password env var name
│   ├── brokers.example.json        # Template; real config/brokers.json is gitignored
│   ├── logging_config.py           # JSON logs, rotating files (log dir per instance)
│   ├── openai_config.py            # LLM client config
│   └── prompts/                    # signal_parser.txt, context_analyzer.txt
│
├── src/
│   ├── telegram_client/            # Telethon wrapper, auth/session, message handler, rate limiter
│   │   ├── client.py, auth_manager.py, session_manager.py
│   │   ├── message_handler.py, rate_limiter.py, errors.py
│   │
│   ├── signal_parser/
│   │   ├── __init__.py             # ParsedSignal, ParsedAction, ValidationResult
│   │   ├── patterns.py             # Regex (French/English): actions, SL/TP lines, zones, follow-ups,
│   │   │                           #   candle-close timeframe, SL range, candle-close remark
│   │   ├── regex_parser.py         # Fast path (combined + component parsing, multi-TP, OBJECTIF)
│   │   ├── llm_parser.py           # gpt-5-mini fallback (circuit breaker, cache)
│   │   ├── context_analyzer.py     # LLM analysis of correlated follow-ups
│   │   └── symbol_policy.py        # Asset-specific values ONLY here (tolerances, buffers, aliases)
│   │
│   ├── correlation_engine/
│   │   ├── correlator.py, reply_tracer.py, time_matcher.py   # Link follow-ups to executed signals
│   │   ├── setup_aggregator.py     # Multi-message setups (PendingSetup), multi-TP ladder legs,
│   │   │                           #   zone watches (ZoneWatch), follow-up extraction
│   │   └── rejection_detector.py   # Pure: zone sweep → reclaim → confirmation on M1/M3 candles
│   │
│   ├── risk_manager/
│   │   ├── risk_guard.py           # Account-wide checks before EVERY order (kill switch, legs,
│   │   │                           #   open risk, daily loss, AutoTrading, account mode/login)
│   │   ├── position_sizer.py       # Risk-based lot per leg
│   │   ├── break_even.py, position_modifier.py, close_processor.py, context_processor.py
│   │
│   ├── mt5_executor/
│   │   ├── connection.py           # Single MT5 session per process; terminal path required,
│   │   │                           #   never switches accounts, demo/real check; prices, candles
│   │   ├── position_manager.py     # Orders, SL/TP modify, close, sizing inputs, risk snapshot
│   │   └── symbol_specs.py
│   │
│   ├── database/                   # SQLAlchemy async + repository pattern (only DB access point)
│   │   ├── __init__.py             # get_database_url(): data/<instance>/signal_ea.db
│   │   ├── models.py, repository.py, cache.py (LLM cache), migrations/
│   │
│   ├── monitoring/                 # health_checker.py, console_dashboard.py (Rich TUI),
│   │                               #   metrics_collector.py, alert_manager.py (not wired yet)
│   ├── utils/                      # circuit_breaker.py, decorators.py, formatters.py,
│   │                               #   currency_config.py, emergency_stop.py, rate_limiter.py
│   ├── models/, queue_manager/     # Placeholders
│
├── scripts/
│   ├── replay_zone.py              # Replay a zone hint on MT5 history (rejection detector)
│   ├── migrate_database.py, benchmark_db.py
│
├── tests/
│   ├── unit/test_<module>.py       # e.g. test_setup_aggregator.py, test_risk_guard.py,
│   │                               #   test_broker_instances.py, test_rejection_detector.py
│   ├── integration/test_<feature>_flow.py
│   └── conftest.py, fixtures/
│
└── docs/
    ├── roadmap.md                  # Current plan (steps, order of work)
    ├── prd/, architecture/         # Sharded PRD and architecture (BMad)
    └── stories/                    # BMad stories
```

## Runtime data (gitignored)

One folder per broker instance (`BOT_INSTANCE`, key from `config/brokers.json`). Without an instance
the same files sit directly in `data/` and `logs/`.

```plaintext
data/
├── llm_cache.db                    # Shared by all instances
├── STOP_TRADING                    # Kill switch (create to block new entries on all instances)
└── <broker>/
    ├── signal_ea.db                # Signals, positions
    ├── telegram.session            # Telegram login of this instance
    ├── signal_ea.pid               # While started via cli.py
    └── state/                      # Health/metrics snapshots
logs/<broker>/app.log, error.log, trades.log

C:\MetaTrader5_Robot_<Broker>\     # Robot MT5 terminal, started with /portable
└── logs\<date>.log                 # MT5 journal (orders actually executed)
```
