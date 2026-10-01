# Telegram Expense Tracker 💰

A personal Telegram bot for logging expenses into a Google Sheet. You type `120 coffee #food @with friends` and it becomes a row in your sheet. You can list, filter, edit, delete and total your spending without leaving the chat.

It's built for one person, or a few people you trust, sharing one sheet. Access is limited to an allowlist of Telegram user ids.

## Usage

| You send | What happens |
|---|---|
| `120 coffee #food @with friends` | Asks for confirmation, then appends amount `120`, description `coffee`, category `food`, notes `with friends` |
| `1,200 rent #home` | Separators are understood (`1,200`, `1,20,000`, `1,234.50`) |
| `/list [n]` | The last *n* entries (default 10, max 50), with Update and Delete buttons |
| `/list_filter` | Step-by-step filter by date, category, amount or description |
| `/list_filter date last month` | Text filters. See `/help_filters` for every format |
| `/summary [period]` | Totals per category. Default is this month. Also takes `last month`, `August 2025`, `01-08-2025 to 15-08-2025`, … |
| `/update <row> <expense>` · `/delete <row>` | Edit or remove a row by its sheet row number |
| `/list_categories` · `/sheet` · `/help` | Categories used so far, a link to the sheet, and help |

Dates accept `dd-mm-yyyy`, `yyyy-mm-dd`, `mm-yyyy`, month names (`Aug 25`), `today`, `yesterday`, `this week`, `last week`, `this month`, `last month`, and ranges joined with `to`.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Create a Google Cloud service account and download its JSON key. Make a sheet with the header row `Timestamp | Amount | Reason | Category | Notes | User`, and share it with the service account's email.
3. Copy `.env.example` to `.env` and fill it in. Leave `ALLOWED_USER_IDS` empty at first.
4. Run it:
   ```sh
   pip install -r requirements.txt
   python bot.py
   ```
5. Message the bot. It replies that it's private and tells you your user id. Put that id in `ALLOWED_USER_IDS` and restart.

The `Procfile` runs it as a long-polling worker, which suits Heroku, Railway and similar hosts.

## How it works

- `bot.py`: Telegram handlers. A guard runs before every other handler and drops updates from users who aren't allowlisted.
- `expenses.py`: pure logic with no Telegram or Google calls: message and amount parsing, date ranges, filters, summaries, Markdown escaping.
- Each command reads the sheet once (`get_all_values`). Appends use `append_row`, and deletes remove exactly one row.
- Conversation state, such as a pending confirmation or a half-built filter, lives in memory per chat. A restart only cancels flows that were in progress, never data.

## Tests

```sh
pip install pytest
python -m pytest tests
```

`tests/test_expenses.py` covers parsing, dates, filters and summaries. `tests/test_bot_smoke.py` imports the bot against a fake sheet and drives the handlers: the access guard, the add confirmation, list, delete, filters and summary.

## Limitations

- One sheet, shared by everyone on the allowlist. There are no per-user ledgers.
- Amounts are shown in ₹, and there's no currency handling.
