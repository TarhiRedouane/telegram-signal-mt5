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
- Full unit suite hangs (> 5 min); 3 tests fail on the clean code
  (`test_telegram_callback_correlates_before_attempting_new_signal_parse`, two in `test_llm_parser.py`).
- Edits to already-executed signals are ignored (logged only).

Order of work: **1 → 4 → 3 → 6 → 8 → 9**, the rest in any order. Each step ships on its own.

---

## Phase 1: Safety (before any new feature)

### 1. Risk guard
- [ ] One check before every order (instant, setup leg, zone, reinforcement).
- [ ] Max total open risk, e.g. 6 % of balance (sum of each open leg's loss at its SL).
- [ ] Max open legs, e.g. 6.
- [ ] Daily loss limit, e.g. 6 %: blocks new entries until the next trading day.
- [ ] Manual kill switch (env var or file flag) that blocks new entries without stopping the bot.
- [ ] Rejected order logged with the reason, and alerted (step 4).

Settings: `MAX_OPEN_RISK_PERCENT`, `MAX_OPEN_LEGS`, `DAILY_LOSS_LIMIT_PERCENT`, `TRADING_ENABLED`.
Done when: unit test proves an order beyond each limit is refused; regular signals still fire under the limits.

### 2. Spread filter
- [ ] Before each entry, read bid/ask; skip (or retry next poll) when spread > per-symbol max.
- [ ] Max spread lives in `symbol_policy.py` (asset-specific values stay there).

Done when: entry is delayed during a wide spread and fires once it normalises (unit test with mocked tick).

### 3. Persist state across restarts
- [ ] Store setups, zone watches, ladder legs, `planned_legs`, `be_done` in the database (repository pattern).
- [ ] Reload at startup, restart watcher tasks, re-check open legs against MT5.
- [ ] Drop entries whose positions no longer exist in MT5.

Done when: kill the bot mid-ladder, restart, TP1 hit → remaining legs still go to break-even.

### 4. Alerts to Telegram DM
- [ ] Wire `src/monitoring/alert_manager.py` into `main.py`.
- [ ] Events: fill (leg n/N, volume, SL, TP), break-even applied, SL/TP hit, zone phase change,
      order rejected (broker or risk guard), watcher error, MT5 disconnect.
- [ ] Respect Telegram rate limiting (`rate_limiter.human_delay()`).

Done when: every event above produces one readable DM on the demo account.

### 5. Test suite health
- [ ] Find and fix the hanging test(s) so `pytest tests/unit` finishes in < 2 min.
- [ ] Fix or remove the 3 failing tests.
- [ ] Stop the noisy `'NoneType' object has no attribute 'upper'` error when the LLM returns no action.
- [ ] Optional: GitHub Actions running `pytest tests/unit`, `ruff`, `mypy src/`.

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

### 16. Live status page
- [ ] Open setups, zone phases, legs, risk used vs limits on one screen.

### 17. Multi-account and MT4 (parked)
- [ ] Revisit after step 3. Constraints found: MetaTrader5 Python lib = one terminal per process;
      MT4 has no Python API (needs an EA bridge or a cloud API); DB needs an account column and
      per-account execution claims.

---

## Rules that apply to every step
- Complete single-message signals and multi-message setups must keep working (regression tests first).
- Asset-specific values only in `symbol_policy.py`.
- Repository pattern for DB, circuit breaker on MT5/OpenAI calls, regex before LLM.
- Telegram + MT5 code: narrow, observable changes; test on the demo account before live.
