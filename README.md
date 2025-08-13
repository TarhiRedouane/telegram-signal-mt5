# Telegram Signal EA

Automated Telegram signal parser and MT5 trading execution system with context-aware risk management.

## Overview

This system monitors Telegram channels for trading signals (primarily GOLD/XAUUSD), parses them using regex patterns and LLM validation, then executes trades on MetaTrader5 with automated risk management including break-even automation and position modifications.

## Features

- **Signal Processing**: Real-time Telegram message monitoring with dual-stage parsing (regex + LLM)
- **Context Correlation**: Links related messages (entries, exits, modifications) across time
- **MT5 Integration**: Automated trade execution with position management
- **Risk Management**: Break-even automation, SL/TP modifications, position sizing
- **Monitoring**: Health checks, metrics collection, and console dashboard

## Requirements

- Python 3.12.7 or higher
- MetaTrader5 terminal (Windows/Wine)
- Telegram API credentials
- OpenAI API key (for LLM parsing)

## Installation

### 1. Clone Repository

```bash
git clone <repository-url>
cd telegram-signal-ea
```

### 2. Create Virtual Environment

```bash
python3.12 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

Required environment variables:
- `TELEGRAM_API_ID`: Your Telegram API ID
- `TELEGRAM_API_HASH`: Your Telegram API hash
- `PHONE_NUMBER`: Your phone number with country code
- `OPENAI_API_KEY`: Your OpenAI API key
- `MT5_LOGIN`: MetaTrader5 login
- `MT5_PASSWORD`: MetaTrader5 password
- `MT5_SERVER`: MetaTrader5 server

### 5. Setup Telegram Session

```bash
python scripts/setup_session.py
```

This will prompt for phone number verification and create the session file.

### 6. Test MT5 Connection

```bash
python scripts/test_mt5_connection.py
```

Verify MT5 connection and account details.

## Usage

### Start the Application

```bash
python main.py
```

Or using the CLI:

```bash
python cli.py start
```

### Check Status

```bash
python cli.py status
```

### Stop the Application

```bash
python cli.py stop
```

## Development

### Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/

# Integration tests only  
pytest tests/integration/

# With coverage
pytest --cov=src
```

### Code Quality

```bash
# Format code
black src/ tests/

# Lint code  
ruff check src/ tests/

# Type check
mypy src/
```

### Database Migrations

```bash
python scripts/migrate_database.py
```

## Project Structure

```
telegram-signal-ea/
├── src/                    # Main source code
│   ├── telegram_client/    # Telegram integration
│   ├── signal_parser/      # Signal parsing logic
│   ├── mt5_executor/       # MT5 trading execution
│   ├── correlation_engine/ # Message correlation
│   ├── risk_manager/       # Risk management
│   └── database/          # Data persistence
├── config/                # Configuration files
├── tests/                 # Test suite
├── scripts/              # Utility scripts
├── logs/                 # Log files (gitignored)
├── data/                 # Database files (gitignored)
├── main.py              # Application entry point
└── cli.py               # CLI interface
```

## Configuration

### Logging

Logs are written to:
- `logs/app.log` - Application logs (rotating, 10MB max)
- `logs/error.log` - Error logs (rotating, 5MB max)  
- `logs/trades.log` - Trade execution logs (rotating, 20MB max)

Set `DEV_MODE=true` in `.env` to enable console output.

### Trading Parameters

Configure in `.env`:
- `DEFAULT_RISK_PERCENT` - Risk per trade (default: 1.0%)
- `DEFAULT_SL_PIPS` - Stop loss in pips (default: 20)
- `DEFAULT_TP_PIPS` - Take profit in pips (default: 30)
- `BREAK_EVEN_TRIGGER_PIPS` - When to move SL to break-even (default: 15)

### Rate Limiting

- `TELEGRAM_RATE_LIMIT_DELAY` - Delay between Telegram requests (default: 2.0s)
- `LLM_RATE_LIMIT_RPM` - OpenAI requests per minute (default: 60)

## Monitoring

The application includes built-in monitoring:

- Health checks every 5 minutes
- Metrics export every hour  
- Console dashboard with real-time status
- Error alerting via logging

## Security

- Never commit `.env` files or session files
- API keys and credentials stored in environment variables
- Telegram sessions encrypted by Telethon
- Database contains no sensitive credentials

## License

MIT License - See LICENSE file for details

## Support

For issues and questions:
1. Check the logs in `logs/` directory
2. Review configuration in `.env`
3. Test individual components using scripts in `scripts/`

## Deployment

For production deployment:
1. Use a VPS with Python 3.12.7
2. Install MetaTrader5 (Windows/Wine required)
3. Setup supervisor for process monitoring
4. Configure log rotation
5. Set up monitoring and alerting