import asyncio
from datetime import datetime
from src.database.models import Base, ParsedAction, ParserType, SignalStatus
from src.database.repository import DatabaseManager, SignalRepository

async def main():
    manager = DatabaseManager('sqlite+aiosqlite:///:memory:')
    await manager.initialize()
    async with manager.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    repo = SignalRepository(manager)
    payload = {
        'telegram_message_id': 37,
        'telegram_chat_id': -1004311007519,
        'sender': 'test_user',
        'timestamp': datetime.utcnow(),
        'raw_text': 'sell gold 4285 SL 4385 TP 4185',
        'parsed_action': ParsedAction.SELL,
        'symbol': 'GOLD',
        'entry_price': 4285.0,
        'stop_loss': 4385.0,
        'take_profit': 4185.0,
        'confidence_score': 0.95,
        'parser_type': ParserType.REGEX,
        'status': SignalStatus.PENDING,
    }
    first = await repo.save_signal(payload)
    second = await repo.save_signal(payload)
    count = await repo.count()
    print({'first': first, 'second': second, 'count': count})
    await manager.close()

asyncio.run(main())
