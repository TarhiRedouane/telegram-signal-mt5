# Step 20 – Manual Tests on the Demo Robot Terminal

Checks that unit tests cannot cover for the step-20 stories: they need live Telegram, a live MT5 terminal or a real process.
Run them on the **demo robot terminal** only (memory "Broker instances": live MT5 is manual-only).

## Order of work

| # | Test | Story | Why | Status of the story |
|---|---|---|---|---|
| T1 | Result posts never start a setup | 20.13 | **Required** (AC 12), blocks Done | Ready for Review |
| T2 | Setup leg waits when too close to the SL | 20.12 | **Required** (AC 15), blocks Done | Ready for Review |
| T3 | Single signal sized on live price, SL moved | 20.7 | Recommended | Done |
| T4 | Fill price stored as `open_price` | 20.4 | Recommended (free, runs with T1-T3) | Done |
| T5 | Break-even retried after a refused modify | 20.2 | Recommended before live | Done |
| T6 | Locked DB: fill tracked, next leg opens | 20.9 + 20.11 | Recommended before live (high-risk rule) | Done |
| T7 | MT5 terminal closed at startup | 20.5 | Optional | Done |
| T8 | `cli.py stop` never kills another program | 20.6 | Optional | Done |
| T9 | Telegram polling survives a network cut | 20.8 | Optional | Done |
| T10 | New French signal is not a follow-up | 20.1 | Optional | Done |
| T11 | Opposite "now" during an open trade | 20.14 | **After 20.14 is built** (AC 16) | Draft |

No manual check needed: 20.3 (chat-scoped IDs, unit-tested); 20.10 (demo run already recorded 2026-10-15, ticket 55167224).

## Before every session

1. Pull the code to test and restart the bot on the demo broker: `python cli.py` → pick the demo broker (e.g. `raise`).
2. Open `logs/<broker>/app.log` (tail it) and the MT5 **Journal** + **Trade** tabs.
3. Check that MT5 **Algo Trading** is ON and no position is open.
4. Use the **test channel** only.

Notation: **P** = current GOLD bid in MT5. Gold: `1.0` = 10 pips (`2.0` = 20 pips, `30.0` = 300 pips).
Wait 1-2 s between posts. Between tests, close open positions by hand and wait 5 min (setup idle timeout) or restart the bot.

---

## T1 – Result posts never start a setup (20.13, required)

**Pass A – empty chat (AC 12)**

| # | Post | Expected in app.log | Expected in MT5 |
|---|---|---|---|
| 1 | `Gold sell setup closed in profit` | **No** `Setup started … awaiting entry` (`Parse failed - no patterns matched` is fine) | nothing |
| 2 | `4150` (any bare number) | no setup line | nothing |
| 3 | `sell gold now` | `Setup started chat=… SELL GOLD, awaiting entry` | nothing yet |
| 4 | P rounded down (e.g. `4146`) | `Setup entry received …`, then `Setup firing …` | **exactly 1 SELL** |

**Pass B – right after A, SELL still open (AC 4)**

| # | Post | Expected in app.log | Expected in MT5 |
|---|---|---|---|
| 5 | `TP1 HIT SELL GOLD +130 PIPS ✅` | **No** `Setup re-armed` | nothing |
| 6 | P rounded down | no `Setup firing` | still 1 SELL |

**Pass C – SELL still open (AC 3)**

| # | Post | Expected in app.log | Expected in MT5 |
|---|---|---|---|
| 7 | `Gold buy closed BE` | **No** `Replacing setup in chat …` | SELL keeps its SL/TP |

- [ ] Pass A  - [ ] Pass B  - [ ] Pass C — ticket: ______

## T2 – Setup leg waits when too close to the SL (20.12, required)

Minimum SL distance for gold: `2.0` (`GOLD_MIN_SL_DISTANCE`).

| # | Post | Expected in app.log | Expected in MT5 |
|---|---|---|---|
| 1 | `sell gold P` / `SL P+1.5` / `TP P-5` / `TP P-10` (one message) | `Setup leg waiting chat=… price … is 1.5 from SL … (minimum 2.0)` | **no order** |
| 2 | `annule` | setup cancelled | nothing |
| 3 | `sell gold P` / `SL P+10` / `TP P-5` / `TP P-10` (one message) | `Setup firing …`, legs filled | legs open (one per TP) |

- [ ] Step 1 waits, no order  - [ ] Step 3 opens the legs — tickets: ______

## T3 – Single signal sized on live price, SL moved (20.7)

Complete single message (one TP), so it takes the instant path.

| # | Post | Expected in app.log | Expected in MT5 |
|---|---|---|---|
| 1 | `sell gold P-5` / `SL P+1.5` / `TP P-20` | `SL moved P+1.5 -> …` (about P+2.0) and `sized on live price …` | 1 SELL, SL about 2.0 above the fill |
| 2 | (after closing #1) `sell gold P` / `SL P+10` / `TP P-20` | `sized on live price …`, **no** `SL moved` | 1 SELL with SL P+10 unchanged |

- [ ] SL moved and order opened  - [ ] normal signal unchanged

## T4 – Fill price stored as `open_price` (20.4)

Run on any fill from T1-T3, no extra posts.

1. app.log shows `Ticket N filled at X (aimed Y)`. If every fill shows `fill price unknown`, report it (broker returns 0.0).
2. DB check (bot can stay running):
   ```bash
   python -c "import sqlite3; c=sqlite3.connect('data/<broker>/signal_ea.db'); print(c.execute('select mt5_ticket, open_price from positions order by id desc limit 3').fetchall())"
   ```
3. `open_price` of ticket N = **Open price** of ticket N in the MT5 Trade tab.

- [ ] Log line present  - [ ] DB `open_price` = MT5 open price

## T5 – Break-even retried after a refused modify (20.2)

Forces a modify failure by switching **Algo Trading off** when TP1 fills (MT5 refuses SL changes from the bot, retcode 10027).

| # | Action | Expected |
|---|---|---|
| 1 | Post `sell gold P` / `SL P+10` / `TP P-1` / `TP P-15` | 2 legs open |
| 2 | Just before TP1 (P-1) is hit, turn **Algo Trading OFF** | — |
| 3 | TP1 hits (leg 1 closes server-side) | WARNING `TP1 hit chat=…: break-even pending for tickets [...], retrying each poll` |
| 4 | Turn **Algo Trading ON** | INFO `TP1 hit chat=…: open legs moved to break-even …`; leg 2 SL = its entry in MT5 |

- [ ] Pending WARNING seen  - [ ] Break-even applied after Algo Trading back on

## T6 – Locked DB: fill tracked, next leg opens (20.9 + 20.11)

Holds an exclusive lock on the instance DB for 60 s, so the bot's writes fail with `database is locked`.

| # | Action | Expected |
|---|---|---|
| 1 | In a second terminal, start the lock:<br>`python -c "import sqlite3,time; c=sqlite3.connect('data/<broker>/signal_ea.db'); c.execute('BEGIN EXCLUSIVE'); print('locked'); time.sleep(60)"` | prints `locked` |
| 2 | Within 60 s, post `sell gold P` / `SL P+10` / `TP P-5` / `TP P-10` | ERROR `Ticket N filled but its position was not saved (…locked…); tracking it in memory, DB row missing` |
| 3 | Watch the next leg | **leg 2 still opens** (MT5 Journal shows both orders) |
| 4 | After the lock ends, let TP1 hit (or move on) | break-even still applied to the open leg (position tracked in memory) |

- [ ] Fill logged as unsaved, bot keeps running  - [ ] Leg 2 opened  - [ ] Break-even still works

## T7 – MT5 terminal closed at startup (20.5)

| # | Action | Expected in app.log |
|---|---|---|
| 1 | Close the robot terminal, start the bot | WARNING at startup; fast `Failed to get current price …` lines, **no 60 s gaps** |
| 2 | Open the robot terminal (no bot restart) | `MT5 connection established after N failed attempts` within about 90 s |

- [ ] Startup continues  - [ ] Reconnects without restart

## T8 – `cli.py stop` never kills another program (20.6)

| # | Action | Expected |
|---|---|---|
| 1 | `python cli.py start <broker>`, then `python cli.py status` | running |
| 2 | `python cli.py stop <broker>` | stopped |
| 3 | Open Notepad; get its PID: `Get-Process notepad \| Select Id` | — |
| 4 | Write that PID into `data/<broker>/signal_ea.pid`, run `python cli.py stop <broker>` | "not running", WARNING naming `notepad.exe` in the CLI log, **Notepad still open** |
| 5 | Delete `data/<broker>/signal_ea.pid` | — |

- [ ] start/status/stop work  - [ ] Notepad survives

## T9 – Telegram polling survives a network cut (20.8)

| # | Action | Expected in app.log |
|---|---|---|
| 1 | Bot running, disconnect the network about 1 min | ERROR `Telegram polling failed chat=…` (one per pass) |
| 2 | Reconnect | INFO `Telegram polling recovered after N failed pass(es)` |
| 3 | Post `sell gold now` in the test channel | `Setup started …` (messages still arrive) |

- [ ] Polling recovers without restart

## T10 – New French signal is not a follow-up (20.1)

| # | Action | Expected |
|---|---|---|
| 1 | Open a trade: `sell gold P` / `SL P+10` / `TP P-20` | 1 SELL |
| 2 | Within a few minutes, **not as a reply**, post a new complete French signal: `je vends gold P` / `SL P+10` / `TP P-20` | handled as a **new** signal (`sized on live price …`, second SELL), no correlation/modify of ticket 1 |

- [ ] Second signal opens its own trade; ticket 1 untouched

## T11 – Opposite "now" during an open trade (20.14, after it is built)

Needs a **hedging** account. Steps from story 20.14 AC 16; re-read the story once built, the final log lines will be in its Dev Agent Record.

| # | Action | Expected |
|---|---|---|
| 1 | Open a SELL through the test channel | 1 SELL |
| 2 | Market under 30.0 (300 pips) from the SELL entry: post `buy gold now` + P | refused (WARNING), SELL setup unchanged |
| 3 | Market 30.0 or more from the SELL entry: post `buy gold now` + P | BUY opens at **half risk**, TP 100 pips; SELL still gets break-even at its TP1 |

- [ ] Refused under 300 pips  - [ ] Half-risk BUY at 300+ pips, SELL still managed

---

## Results log

Copy the app.log lines into the story's Dev Agent Record (or send them to Dev/QA) and tick the story.

| Date | Test | Broker | Result | Tickets | Notes |
|---|---|---|---|---|---|
| | | | | | |
