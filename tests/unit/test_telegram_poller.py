"""Story 20.8: the Telegram polling fallback survives errors (review F36).

`_poll_telegram_groups` runs for real against a fake Telethon client. `asyncio.sleep` is replaced by
a recorder that ends the loop with `CancelledError` after a set number of calls, so no test waits on
real time. `get_peer_id` is patched so plain namespaces work as entities (chat id = -(1000 + id)).
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError

import main

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
A = SimpleNamespace(id=1, title="Group A")
B = SimpleNamespace(id=2, title="Group B")


def _msg(msg_id, text=None):
    return SimpleNamespace(
        id=msg_id,
        text=text if text is not None else f"m{msg_id}",
        date=NOW,
        from_id=None,
        reply_to=None,
        forward=None,
        edit_date=None,
    )


def _setup(monkeypatch, script, sleeps_before_stop, real_handler=False):
    """script: {entity_id: [response, ...]}, one response per get_messages call (seed or poll);
    a response is a message list or an exception to raise. Empty queue -> no messages.
    The sleep recorder raises CancelledError on call number `sleeps_before_stop + 1`."""
    calls = []

    async def get_messages(entity, limit, min_id=None):
        calls.append((entity.id, min_id, limit))
        queue = script[entity.id]
        response = queue.pop(0) if queue else []
        if isinstance(response, BaseException):
            raise response
        return response

    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) > sleeps_before_stop:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    monkeypatch.setattr(main, "get_peer_id", lambda entity: -(1000 + entity.id))

    app = main.ApplicationManager()
    app.telegram_client = SimpleNamespace(client=SimpleNamespace(get_messages=get_messages))
    app._connected_group_entities = [A, B]
    app.logger = MagicMock()
    if not real_handler:
        app._on_telegram_message = AsyncMock()
    return app, calls, delays


async def _run(app):
    with pytest.raises(asyncio.CancelledError):
        await app._poll_telegram_groups()


def _logged(mock_level):
    return [str(call.args[0]) for call in mock_level.call_args_list]


def _processed_ids(app):
    return [call.args[0]["telegram_message_id"] for call in app._on_telegram_message.call_args_list]


async def test_happy_path_seeds_then_polls_in_id_order(monkeypatch):
    script = {
        1: [[_msg(10)], [_msg(12), _msg(11)], []],
        2: [[], [_msg(5)], []],  # empty group seeded with 0, as before
    }
    app, calls, delays = _setup(monkeypatch, script, sleeps_before_stop=2)

    await _run(app)

    assert calls == [
        (1, None, 1),
        (2, None, 1),
        (1, 10, 20),
        (2, 0, 20),
        (1, 12, 20),
        (2, 5, 20),
    ]
    assert delays == [3, 3, 3]
    assert _processed_ids(app) == [11, 12, 5]
    assert app._on_telegram_message.call_args_list[0].args[0] == {
        "telegram_message_id": 11,
        "telegram_chat_id": -1001,
        "sender": "unknown_sender",
        "timestamp": NOW,
        "raw_text": "m11",
        "reply_to_message_id": None,
        "chat_title": "Group A",
        "is_forwarded": False,
        "message_type": "text",
        "edit_date": None,
    }
    info = _logged(app.logger.info)
    assert any("Telegram polling seed chat=-1001" in m and "last_message_id=10" in m for m in info)
    assert any("Telegram polling seed chat=-1002" in m and "last_message_id=0" in m for m in info)
    assert "Telegram polling fallback stopped" in info
    app.logger.error.assert_not_called()


async def test_poll_error_is_logged_and_polling_continues(monkeypatch):
    script = {
        1: [[_msg(10)], ConnectionError("connection dropped"), [_msg(11)]],
        2: [[_msg(20)], [], []],
    }
    app, calls, delays = _setup(monkeypatch, script, sleeps_before_stop=2)

    await _run(app)

    assert calls[2:] == [(1, 10, 20), (2, 20, 20), (1, 10, 20), (2, 20, 20)]
    errors = _logged(app.logger.error)
    assert len(errors) == 1
    assert "chat=-1001" in errors[0] and "connection dropped" in errors[0]
    assert _processed_ids(app) == [11]
    assert delays == [3, 6, 3]
    assert sum("recovered" in m for m in _logged(app.logger.info)) == 1


async def test_flood_wait_on_poll_waits_its_seconds_then_continues(monkeypatch):
    script = {
        1: [[_msg(10)], FloodWaitError(request=None, capture=7), [_msg(11)]],
        2: [[_msg(20)], [], []],
    }
    app, calls, delays = _setup(monkeypatch, script, sleeps_before_stop=3)

    await _run(app)

    # The flood wait ends the pass: B is not polled until the next one
    assert calls[2:] == [(1, 10, 20), (1, 10, 20), (2, 20, 20)]
    assert delays == [3, 7, 6, 3]
    warnings = _logged(app.logger.warning)
    assert len(warnings) == 1 and "7" in warnings[0]
    app.logger.error.assert_not_called()
    assert _processed_ids(app) == [11]


async def test_flood_wait_on_seed_waits_then_seeds_again(monkeypatch):
    script = {
        1: [FloodWaitError(request=None, capture=7), [_msg(10)], [_msg(11)]],
        2: [[_msg(20)], []],
    }
    app, calls, delays = _setup(monkeypatch, script, sleeps_before_stop=2)

    await _run(app)

    # B is not seeded during the flood wait; both seeded on the next pass, never polled from 0
    assert calls == [(1, None, 1), (1, None, 1), (2, None, 1), (1, 10, 20), (2, 20, 20)]
    assert delays == [7, 6, 3]
    assert any("7" in m for m in _logged(app.logger.warning))
    assert _processed_ids(app) == [11]


async def test_failed_passes_back_off_and_reset_on_recovery(monkeypatch):
    script = {
        1: [[_msg(10)], *[ConnectionError("down") for _ in range(3)], []],
        2: [[_msg(20)]],
    }
    app, _, delays = _setup(monkeypatch, script, sleeps_before_stop=5)

    await _run(app)

    assert delays == [3, 6, 12, 24, 3, 3]
    assert sum("recovered" in m for m in _logged(app.logger.info)) == 1
    # Full traceback only on the first failure of the streak
    assert [call.kwargs.get("exc_info") for call in app.logger.error.call_args_list] == [
        True,
        False,
        False,
    ]


async def test_back_off_is_capped_at_60_seconds(monkeypatch):
    script = {1: [[_msg(10)], *[ConnectionError("down") for _ in range(7)]], 2: [[_msg(20)]]}
    app, _, delays = _setup(monkeypatch, script, sleeps_before_stop=7)

    await _run(app)

    assert delays == [3, 6, 12, 24, 48, 60, 60, 60]
    assert not any("recovered" in m for m in _logged(app.logger.info))


async def test_failed_seed_is_retried_and_group_never_polled_from_zero(monkeypatch):
    script = {
        1: [ConnectionError("seed failed"), [_msg(10)], [_msg(11)]],
        2: [[_msg(20)], [_msg(21)], []],
    }
    app, calls, delays = _setup(monkeypatch, script, sleeps_before_stop=1)

    await _run(app)

    assert (1, 0, 20) not in calls
    assert calls == [(1, None, 1), (2, None, 1), (1, None, 1), (1, 10, 20), (2, 20, 20)]
    assert delays == [6, 3]
    errors = _logged(app.logger.error)
    assert len(errors) == 1 and "chat=-1001" in errors[0] and "seed failed" in errors[0]
    assert _processed_ids(app) == [11, 21]


async def test_bad_message_is_logged_and_not_processed_twice(monkeypatch):
    script = {
        1: [[], [_msg(1), _msg(2, "bad"), _msg(3)], [_msg(2, "bad"), _msg(3)], []],
        2: [[]],
    }
    app, calls, delays = _setup(monkeypatch, script, sleeps_before_stop=3, real_handler=True)

    # Real _on_telegram_message (real dedupe); it returns early on a zone hint
    seen_texts = []

    def zone_hint(text):
        seen_texts.append(text)
        if text == "bad":
            raise RuntimeError("boom")
        return ("BUY", 1, 2)

    monkeypatch.setattr(main, "extract_zone_hint", zone_hint)
    app._start_zone_watch = MagicMock()

    await _run(app)

    polls_a = [c for c in calls if c[0] == 1 and c[1] is not None]
    assert polls_a == [(1, 0, 20), (1, 1, 20), (1, 3, 20)]
    assert seen_texts == ["m1", "bad", "m3"]  # message 2 processed once
    watched = [call.args[0]["telegram_message_id"] for call in app._start_zone_watch.call_args_list]
    assert watched == [1, 3]
    errors = _logged(app.logger.error)
    assert len(errors) == 1
    assert "chat=-1001" in errors[0] and "message_id=2" in errors[0] and "boom" in errors[0]
    assert delays == [3, 3, 3, 3]


async def test_cancel_stops_the_task(monkeypatch):
    app, _, _ = _setup(monkeypatch, {1: [[_msg(10)]], 2: [[]]}, sleeps_before_stop=0)

    await _run(app)

    assert "Telegram polling fallback stopped" in _logged(app.logger.info)


async def test_textless_messages_advance_last_seen_and_metadata_is_unchanged(monkeypatch):
    """Pins the metadata builder and the text-less skip before 19.5 moves the poller (AC 7)."""
    photo = _msg(11, text="")
    from_channel = _msg(12)
    from_channel.from_id = SimpleNamespace(channel_id=77)
    from_channel.reply_to = SimpleNamespace(reply_to_msg_id=11)
    from_channel.forward = object()
    from_user = _msg(13)
    from_user.from_id = SimpleNamespace(user_id=5)
    script = {1: [[_msg(10)], [from_user, photo, from_channel], [_msg(14, text="")], []], 2: [[]]}
    app, calls, _ = _setup(monkeypatch, script, sleeps_before_stop=3)

    await _run(app)

    assert [c for c in calls if c[0] == 1 and c[1] is not None] == [
        (1, 10, 20),
        (1, 13, 20),
        (1, 14, 20),  # a text-less newest message still advances last_seen
    ]
    assert _processed_ids(app) == [12, 13]
    channel_meta, user_meta = (call.args[0] for call in app._on_telegram_message.call_args_list)
    assert channel_meta["sender"] == "channel_77"
    assert channel_meta["reply_to_message_id"] == 11
    assert channel_meta["is_forwarded"] is True
    assert user_meta["sender"] == "user_5"
    assert user_meta["reply_to_message_id"] is None
