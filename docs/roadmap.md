# Roadmap: safer, measurable, smarter execution

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done

## Where we are (2026-10-05)

Working today:
- Complete single-message signals (`sell gold 4149 / SL / TP`) → instant market order.
- Multi-message setups (entry range + SL + TP collected over several messages, including edits).
- Multi-TP ladder: one leg per TP (max 4), spread across the entry zone, scale-out TPs,
  TP1 → break-even on the other legs.
- Risk-based lot size (`RISK_PERCENT`, default 3 % per signal, split across legs).
- Zone hints (`BUY ZONE 4174–4178`) traded only after a confirmed rejection on M1/M3 candles
  (sweep → reintegration → displacement), plus `scripts/replay_zone.py` to replay a zone on history.
- LLM guard: no buy/sell word → no trade.

Known gaps:
- No limit on total open risk, number of positions, or daily loss. Several setups can each risk 3 %,
  and a zone reinforcement adds another full 3 %.
- Setups, zone watches, ladder legs and break-even state live in memory only: a restart leaves open
  legs without break-even management.
- Nothing records trade results (profit, R, close reason), so we can't tell which signals work.
- `AlertManager` exists but is never called: the bot acts silently.
- Unit + integration suites green and fast since stories 5.1/5.2 (745 passed on 2026-10-13, ~1.5 min); no CI runs
  them yet. `main.py` coverage is 51 % (see step 19).
- Edits to already-executed signals are ignored (logged only).

Order of work: **1 (done) → 5 (done) → 19 review (done, story 19.1) → 20 (live-path fixes) → 19
refactor (stories 19.2-19.9) → 4 → 3 → 6 → 8 → 9**, then **18** (channels per broker) → **16** (control page), the rest in any order.
Each step ships on its own.

---

## Phase 1: Safety (before any new feature)

### 1. Risk guard
- [x] One check before every order (instant, setup leg, zone, reinforcement).
- [x] Max total open risk, e.g. 6 % of balance (sum of each open leg's loss at its SL).
- [x] Max open legs, e.g. 6.
- [x] Daily loss limit, e.g. 6 %: blocks new entries until the next trading day.
- [x] Manual kill switch (env var or file flag) that blocks new entries without stopping the bot.
- [~] Rejected order logged with the reason (done), and alerted (step 4).

Settings: `MAX_OPEN_RISK_PERCENT`, `MAX_OPEN_LEGS`, `DAILY_LOSS_LIMIT_PERCENT`, `TRADING_ENABLED`,
`KILL_SWITCH_FILE` (default `data/STOP_TRADING`: create it to block entries, delete it to resume).
Code: `src/risk_manager/risk_guard.py`, `PositionManager.get_risk_snapshot`, `main._risk_guard_refusal`.
Done when: unit test proves an order beyond each limit is refused; regular signals still fire under the limits.

### 2. Spread filter
- [ ] Before each entry, read bid/ask; skip (or retry next poll) when spread > per-symbol max.
- [ ] Max spread lives in `symbol_policy.py` (asset-specific values stay there).

Done when: entry is delayed during a wide spread and fires once it normalises (unit test with mocked tick).

### 3. Persist state across restarts
- [ ] Store setups, zone watches, ladder legs, `planned_legs`, `be_done` in the database (repository pattern).
- [ ] Reload at startup, restart watcher tasks, re-check open legs against MT5.
- [ ] Drop entries whose positions no longer exist in MT5.
- [ ] Time-based correlation for late messages: `time_matcher` loads candidate signals from the last
      few minutes before *now* (`get_recent_signals`), not before the message's own timestamp, so a
      follow-up processed late (catch-up after restart/reconnect) never time-matches (found in 5.2 QA).
      Anchor the window on the message timestamp. Reply-chain correlation is not affected.

Done when: kill the bot mid-ladder, restart, TP1 hit → remaining legs still go to break-even.

### 4. Alerts to Telegram DM
- [ ] Wire `src/monitoring/alert_manager.py` into `main.py`.
- [ ] Events: fill (leg n/N, volume, SL, TP), break-even applied, SL/TP hit, zone phase change,
      order rejected (broker or risk guard), watcher error, MT5 disconnect.
- [ ] Respect Telegram rate limiting (`rate_limiter.human_delay()`).

Done when: every event above produces one readable DM on the demo account.

### 5. Test suite health
- [x] Find and fix the hanging test(s) so `pytest tests/unit` finishes in < 2 min.
- [x] Fix or remove the failing tests (there were 146 + 10 errors once `pytest.ini` was really applied).
- [x] Stop the noisy `'NoneType' object has no attribute 'upper'` error when the LLM returns no action.
- [x] Integration suite green, no hang (was 25 failed + 1 hang), and a coverage baseline for
      `main.py` (story 5.2). Full `pytest`: 693 passed, 6 skipped, ~1 min.
- [ ] Optional: GitHub Actions running `pytest tests/unit`, `ruff`, `mypy src/`.
- [ ] LLM rate limiter really waits: `APIRateLimiter.acquire()` queues the request at the limit but
      returns True at once, so OpenAI is called anyway (found in 5.1 QA). Await the queued slot or
      drop the queue. Low urgency: LLM is the fallback path only.
- [x] One formatting-only commit: `black .` (most files were never formatted, so
      `black --check` fails). Do it before step 19 so refactor diffs stay readable. Story 5.3
      (91 files incl. `main.py`, `cli.py`, `config/`, `scripts/`: run `black .`, not only `src/ tests/`).

Done when: full unit suite green and fast.

---

## Phase 2: Measure

### 6. Trade journal
- [ ] Sync closed positions from MT5 deal history: close price, profit, close reason (TP/SL/manual), close time.
- [ ] Store R multiple per position (profit ÷ initial risk).
- [ ] Link each position to its source: chat, message type (instant / setup / ladder leg / zone), leg index.

Done when: every closed demo trade has a complete journal row.

### 7. Performance by source
- [ ] Report (script or command) of win rate, average R, total R, max drawdown by chat,
      message type, session (Asia / London / New York) and weekday.
- [ ] Sessions via stdlib `zoneinfo`: add `tzdata` to requirements (Windows has no system tz database;
      today `tzdata` only comes through `pendulum`, which story 19.3 removes).

Done when: one command prints the table for a date range.

### 8. Full replay harness
- [ ] Extend `scripts/replay_zone.py` into a replay of stored Telegram messages (raw text + timestamp from the DB)
      through the real pipeline (parser → aggregator → ladder → break-even) on MT5 history, with mocked execution.
- [ ] Output the same metrics as step 7.

Done when: a rule change can be compared before/after on the last N weeks of real messages.

---

## Phase 3: Smarter execution

### 9. Volatility-based thresholds
- [ ] Zone max sweep and SL buffer = k × ATR(M3) instead of fixed 3.0 / 0.5 points.
- [ ] Same idea for default SL distance and entry tolerance.
- [ ] Multipliers per symbol in `symbol_policy.py`; tune k with the replay (step 8).

### 10. Limit orders for ladder legs
- [ ] Place pending limit orders for legs 2..N instead of polling every 5 s.
- [ ] Cancel unfilled orders on TP1 hit, setup expiry, cancel message or invalidation.

### 11. Trail after TP2
- [ ] TP2 hit → move remaining legs' SL to TP1.

### 12. Message classifier
- [ ] LLM structured output classifying each message: signal / setup / zone / follow-up / analysis / education.
- [ ] Education and analysis posts (e.g. "COURS LEB LE REJET") are never traded.
- [ ] Regex stays first; classifier only when regex is unsure (cost control).

### 13. Apply edits to executed signals
- [ ] Author edits SL/TP of an executed signal → update its live positions (same direction-sanity rules).

### 14. Risk per source
- [ ] Risk % per chat driven by step 7 stats (e.g. 3 % proven channel, 1 % new/weak one).

---

## Phase 4: Convenience

### 15. Daily report
- [ ] Evening Telegram DM: trades, profit, R, open risk, tomorrow's active zones.

### 16. Control page (one screen for all brokers)
Needs first: step 3 (setups/zones persisted, so another process can read them), step 6 (trade
journal), step 18 (channels per broker). Trading never runs inside the page: bot processes stay
headless 24/7; the page only reads their data and sends commands, so a page crash stops nothing.

v1, read-only:
- [ ] Local web page (`python cli.py dashboard`, http://127.0.0.1 only), no desktop GUI framework.
- [ ] Per broker: running/stopped, account (`DEMO 5011840`), AutoTrading on/off, channels.
- [ ] Open positions, pending setups and zone phases, legs, risk used vs limits, today's P&L.
- [ ] Last messages per channel with the decision taken (traded / setup / zone / ignored + reason).
- [ ] Reads `data/<broker>/` (DB + state) and `config/brokers.json`; no new store.

v2, controls:
- [ ] Start/stop a broker (same code as `cli.py start/stop`), kill switch on/off.
- [ ] Edit channels per broker and non-secret profile fields; writes `config/brokers.json`.
- [ ] Never shows or edits passwords/API keys (stay in `.env`); listens on localhost only. Remote
      access (phone, VPS) only behind an authenticated tunnel/VPN, decided when needed.

Done when: with two brokers running, everything above is visible on one page and start/stop + kill
switch work from it; closing the page changes nothing for running bots.

### 17. Multi-account and MT4
- [x] Several MT5 brokers side by side (2026-10-09): profiles in `config/brokers.json`,
      `python cli.py start` menu, one bot process per broker (`BOT_INSTANCE`) with its own portable
      terminal, `data/<broker>/` (DB, Telegram session, state) and `logs/<broker>/`. Account guards:
      never attach without a terminal path, never switch a terminal's account, demo/real mode checked
      at connect and before every order.
- [ ] MT4 (parked): no Python API, needs an EA bridge or a cloud API.

### 19. Architecture review and simplification
`main.py` has grown to ~1,660 lines / 42 methods (Telegram routing, setups, zones, ladder legs,
candle-close invalidation, sizing, risk guard, MT5 execution, polling, shutdown). Other big files:
`connection.py` 1,426, `repository.py` 1,373, `position_manager.py` 1,331. Review before adding
steps 3/16/18, which all touch `main.py`.

Coverage baseline (story 5.2, 2026-10-09, `pytest --cov=main --cov=src`): `main.py` **48 %**
(483 of 926 statements not run), `main.py` + `src/` **72 %**. Methods in `main.py` that no test runs:
- Trading paths: `_handle_parsed_signal`, `_order_volume`, `_validate_live_price`, `_start_setup`,
  `_apply_setup_levels`, `_disarm_or_drop`, `_handle_setup_edit`, `_handle_setup_followup`,
  `_llm_setup_followup`, `_watch_zone`, `_advance_zones`.
- Lifecycle: `_initialize_mt5_connection`, `run`, `shutdown`, `_save_application_state`,
  `_shutdown_monitoring_components`, `_cancel_component_tasks`, `_shutdown_logging`,
  `signal_shutdown`, `signal_handler`, `main`.
Cover the trading paths (at least the ones a refactor story moves) before moving them.
- [x] Review (architect / tech lead): current structure, responsibilities per file, dead code,
      duplicated logic, unused dependencies (APScheduler, pendulum, aiohttp, aiofiles), modules that
      exist but are bypassed (`close_processor`, `alert_manager`, `queue_manager`, `models`).
      Story 19.1: [architecture/review-2026-10.md](architecture/review-2026-10.md).
- [x] Target structure, e.g. split `main.py` into: app wiring/lifecycle, message router,
      setup/zone orchestration (ladder, legs, invalidation), order execution (sizing + risk guard + MT5).
      Plan: `src/app/` (router, zones, setups, execution) + `telegram_client/poller.py`, stories 19.2-19.9.
- [ ] Refactor in small stories, behaviour unchanged: move code, no new features; each story keeps
      the full unit suite green (needs step 5 first: suite must finish and be green).
- [ ] Delete dead code and unused dependencies found by the review.

Done when: no file over ~600 lines without a reason, `main.py` only wires components, all tests green,
demo run identical to before (same log decisions on a replayed message set).

### 20. Live-path fixes (found by the 19.1 review, before the refactor)
Bug fixes change behaviour, so they are not part of the move-only refactor. Details and evidence:
[architecture/review-2026-10.md](architecture/review-2026-10.md) section 5. Order: money first, smallest first.
- [x] 20.1 One "new trade" rule: a French signal (`JE VENDS GOLD 4150 …`) within 5 min of an open trade in the
      same chat is time-matched as a follow-up and never executed (F20).
- [x] 20.2 Break-even is marked done before it is applied; retry until every open leg's SL moved. Same for the
      invalidation close (F21).
- [x] 20.3 Chat-scoped message ids: unique `(telegram_chat_id, telegram_message_id)`, chat id on every lookup,
      Alembic migration; back up `signal_ea.db` first (F19).
- [x] 20.4 Store the real fill price as `open_price`, not the quote/mid (F22).
- [x] 20.5 MT5 startup retry (no 60 s hang on every call) and cache the resolved broker symbol name (F23, F28).
- [x] 20.6 `cli.py stop` checks the PID's command line before killing (F27).
Found by the review re-run on 2026-10-13 (review section 6), also before 19.2:
- [x] 20.7 Size and risk-check complete single-message signals on the live price, not the post's entry; SL never under
      20 pips from the market (`GOLD_MIN_SL_DISTANCE=2.0`) unless the post's own stop is tighter (F35).
- [ ] Decide (user): tighter live-price deviation rule in `symbol_policy` (`GOLD_MAX_PRICE_DEVIATION` is 100 points);
      left out of 20.7, it changes which signals are traded.
- [x] 20.8 Telegram polling fallback survives errors (per-iteration catch, flood-wait back-off) (F36).
- [x] 20.9 A filled order always yields a tracked position, even when the DB write fails (F37).
Found in the live log on 2026-10-15:
- [ ] 20.10 "sell gold now" waits for the entry price (next message, e.g. "4146"), then fires only when the market is
      within the entry tolerance or better, up to the default SL distance; no entry within 5 min = no trade.
Found by the 20.9 QA review on 2026-10-15 (low; fails safe):
- [ ] 20.11 A failed `EXECUTED` status write after a fill leaves the signal `VALIDATED`, so the setup's next ladder leg
      is refused as "already claimed" and the setup is disarmed with the misleading log "order not filled". Allow the
      extra leg for a signal this setup already filled, or log the real reason (story 20.9, QA Results).
Later steps: F25 (positions never marked closed) → step 6, F26 (one leg updated) → step 13, F29 (filling mode,
magic) → step 17/18, F24 (no per-chat ordering) → story 19.8, F41 (partial fill volume) → step 6,
F40 (author's profit-lock SL ignored) → step 11/13.

Done when: each fix has a test that failed before the fix; full suite green.

### 18. Telegram channels per broker
Each broker instance listens only to the channels configured for it, e.g. VT → A, B, C;
Raise → A, B; another broker → A only, or all of them.
- [ ] Optional `"channels"` list per profile in `config/brokers.json` (chat ids, like `TELEGRAM_GROUPS`).
      No list = today's behaviour: every channel in `TELEGRAM_GROUPS`.
- [ ] Optional `"channels": "all"` = every channel in `TELEGRAM_GROUPS` (explicit form of the default).
- [ ] Validation at startup: a profile channel missing from `TELEGRAM_GROUPS` is reported (or allowed on
      its own; decide when implementing), unknown/invalid ids stop the instance with a clear error.
- [ ] One place decides: `settings.get_telegram_groups()` returns the instance's list, so the live
      listener, the polling fallback (`main._poll_telegram_groups`) and group validation
      (`telegram_client/client.py`) all follow it without separate changes.
- [ ] `cli.py start` menu shows each broker's channels next to its account
      (`1) Raise demo [DEMO 5011840] channels: A, B`); dashboard shows them too.
- [ ] Optional channel names (`{"id": -100…, "name": "LEB VIP"}`) for readable logs and menu.

Done when: two instances with different channel lists run side by side, a signal posted in a channel
only one of them follows is executed by that instance only (test with mocked messages + demo run).

---

## Rules that apply to every step
- Complete single-message signals and multi-message setups must keep working (regression tests first).
- Asset-specific values only in `symbol_policy.py`.
- Repository pattern for DB, circuit breaker on MT5/OpenAI calls, regex before LLM.
- Telegram + MT5 code: narrow, observable changes; test on the demo account before live.
