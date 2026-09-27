# Method-Level Execution Sequence

This diagram focuses on concrete method calls from the runtime entry point to shutdown so code navigation is easier.

## End-to-End Runtime Flow (Method Names)

```mermaid
sequenceDiagram
    autonumber
    participant OS as OS Signal/Console
    participant main as main.main()
    participant app as ApplicationManager
    participant auth as AuthManager
    participant tg as TelegramClient
    participant mh as MessageHandler
    participant regex as RegexParser
    participant llm as LLMParser
    participant mt5 as MT5ConnectionManager
    participant db as SignalRepository/Database
    participant mon as HealthMonitor + ConsoleDashboard

    Note over main,app: Startup lifecycle
    main->>app: startup()
    app->>app: _initialize_runtime_components()
    app->>auth: startup_authentication()
    auth-->>app: telegram_client

    app->>app: _initialize_monitoring_components()
    app->>mon: HealthMonitor.start_monitoring()
    app->>mon: ConsoleDashboard.start_dashboard() (task)

    app->>app: _initialize_persistence_components()
    app->>db: initialize_database(run_migrations=False)
    app->>db: get_signal_repository()

    app->>app: _initialize_telegram_monitoring()
    app->>tg: connect_to_groups()
    tg-->>app: connected groups
    app->>mh: set_message_callback(_on_telegram_message)
    app->>mh: create_event_handler(group_entities)
    app->>tg: client.add_event_handler(handle_new_message, filter)
    app->>app: _poll_telegram_groups() (task)

    app->>app: _initialize_mt5_connection() (task)
    app->>mt5: _ensure_initialized()

    main->>app: run() (wait shutdown_event)

    Note over tg,db: Incoming message path
    tg->>mh: handle_new_message(event)
    mh->>app: _on_telegram_message(metadata)
    app->>regex: parse(raw_text)

    alt RegexParser.parse() returns None
        app->>llm: parse_with_llm(raw_text)
        llm-->>app: parsed_signal or None
    else RegexParser.parse() returns signal
        regex-->>app: parsed_signal
    end

    alt Parsed signal exists
        app->>app: _validate_live_price(parsed_signal)
        app->>mt5: get_current_price(symbol)
        mt5-->>app: current_price

        alt Live price validation passed
            app->>app: _handle_parsed_signal(parsed_signal)
            app->>db: save_signal({...})
            app->>mon: add_recent_signal(...)
            app->>mon: set_message_stats(...)
        else Live price validation failed
            app-->>app: reject/log signal
        end
    else Parsed signal missing
        app-->>app: parse failed/log
    end

    Note over OS,app: Graceful shutdown lifecycle
    OS->>main: SIGINT/SIGTERM
    main->>app: signal_shutdown()
    app-->>main: shutdown_event.set()
    main->>app: shutdown()

    app->>app: _save_application_state()
    app->>app: _shutdown_monitoring_components()
    app->>auth: cleanup()
    app->>mt5: shutdown()
    app->>db: close_database()
    app->>app: _cancel_component_tasks()
    app->>app: _shutdown_logging()
```

## Primary Methods To Trace In Code

- `main.main`
- `ApplicationManager.startup`
- `ApplicationManager._initialize_runtime_components`
- `ApplicationManager._initialize_monitoring_components`
- `ApplicationManager._initialize_persistence_components`
- `ApplicationManager._initialize_telegram_monitoring`
- `ApplicationManager._on_telegram_message`
- `ApplicationManager._validate_live_price`
- `ApplicationManager._handle_parsed_signal`
- `ApplicationManager._poll_telegram_groups`
- `ApplicationManager._initialize_mt5_connection`
- `ApplicationManager.shutdown`