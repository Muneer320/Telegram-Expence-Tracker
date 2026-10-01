#!/usr/bin/env python3
import os
import re
import json
import logging
import gspread
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

from expenses import (filter_entries_by_criteria, format_amount, md, now_timestamp, parse_date_input,
                      parse_message, rows_from_values, summarize)

# Config
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPREADSHEET_KEY = os.getenv("SPREADSHEET_KEY")
GOOGLE_SA_JSON = os.getenv("GOOGLE_SA_JSON")

if not BOT_TOKEN or not SPREADSHEET_KEY or not GOOGLE_SA_JSON:
    raise SystemExit("Missing required env vars: TELEGRAM_BOT_TOKEN, SPREADSHEET_KEY, GOOGLE_SA_JSON")

# Only these Telegram user ids may use the bot (comma-separated). Anyone else is refused
# and told their id, which is how you find the value to put here on first setup.
ALLOWED_USER_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.strip().isdigit()}

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google Sheets
try:
    gc = gspread.service_account_from_dict(json.loads(GOOGLE_SA_JSON))
    sh = gc.open_by_key(SPREADSHEET_KEY)
    ws = sh.sheet1
except Exception as e:
    logger.exception("Failed to initialize Google Sheets client")
    raise

# Per-chat conversation state (in memory; a restart clears unfinished flows, not data)
pending_update = {}
pending_add = {}
current_list_data = {}
interactive_filter_state = {}
interactive_input_state = {}

# ---------------- Google Sheets ----------------
def read_entries():
    """All expense rows in a single Sheets read (one API call, not one per row)."""
    return rows_from_values(ws.get_all_values())


def append_entry(amount, reason, category, notes, user):
    values = [now_timestamp(), amount, reason or "", category or "", notes or "", user or ""]
    result = ws.append_row(values, value_input_option="USER_ENTERED", table_range="A1")
    updated = (result or {}).get("updates", {}).get("updatedRange", "")
    match = re.search(r"![A-Z]+(\d+)", updated)
    return int(match.group(1)) if match else None


def get_last_n_entries(n=5):
    return read_entries()[-n:]


def update_row_exact(row_num, amount, reason, category, notes, user):
    if row_num <= 1:
        raise ValueError("Cannot update header row")
    existing_row = ws.row_values(row_num)
    if not existing_row:
        raise ValueError(f"Row {row_num} is empty")
    values = [existing_row[0], amount or "", reason or "", category or "", notes or "", user or ""]
    ws.update(f"A{row_num}:F{row_num}", [values], value_input_option="USER_ENTERED")


def delete_row_exact(row_num):
    """Delete one sheet row; rows below shift up."""
    if row_num <= 1:
        raise ValueError("Cannot delete header row")
    if row_num > ws.row_count:
        raise ValueError("Row number exceeds sheet data")
    ws.delete_rows(row_num)


def get_all_categories():
    """Get all unique categories from the sheet"""
    try:
        data = ws.get_all_values()
        if len(data) <= 1:
            return []
        categories = set()
        for row in data[1:]:
            if len(row) > 3 and row[3].strip():
                categories.add(row[3].strip())
        return sorted(list(categories))
    except Exception as e:
        logger.exception("Failed to get categories")
        return []

def get_entries_with_filter(n=5, filters=None):
    """Newest-first entries matching the filters, at most n."""
    rows = read_entries()
    if filters:
        rows = filter_entries_by_criteria(rows, filters)
    return sorted(rows, key=lambda x: x['row'], reverse=True)[:n]


def build_filtered_list_message(rows, filter_info=""):
    """Build list message for filtered results"""
    if not rows:
        return f"📭 **No entries found!**{filter_info}\n\nTry adjusting your filters or add some expenses first."

    lines = [f"📊 **Filtered Expenses:**{filter_info}\n"]
    for r in rows:
        # Format each entry nicely
        category = md(r['category']) if r['category'] else 'uncategorized'
        notes = md(r['notes']) if r['notes'] else ""
        reason = md(r['reason']) if r['reason'] else 'expense'

        lines.append(f"**{r['row']}:** `₹{r['amount']}` {reason}")
        lines.append(f"    📅 {r['ts']}")
        lines.append(f"    🏷️ {category}")
        if notes:
            lines.append(f"    📝 {notes}")
        lines.append("")  # Empty line for spacing

    return "\n".join(lines)

def build_list_message_and_keyboard(n=5):
    rows = get_last_n_entries(n)
    if not rows:
        return "📭 **No entries yet!**\n\nStart tracking by sending something like:\n`120 coffee #food`", None

    lines = ["📊 **Your Recent Expenses:**\n"]
    for r in rows:
        # Format each entry nicely
        category = md(r['category']) if r['category'] else 'uncategorized'
        notes = md(r['notes']) if r['notes'] else ""
        reason = md(r['reason']) if r['reason'] else 'expense'

        lines.append(f"**{r['row']}:** `₹{r['amount']}` {reason}")
        lines.append(f"    📅 {r['ts']}")
        lines.append(f"    🏷️ {category}")
        if notes:
            lines.append(f"    📝 {notes}")
        lines.append("")  # Empty line for spacing

    # Simple keyboard with just Update and Delete buttons
    keyboard = [
        [
            InlineKeyboardButton("✏️ Update", callback_data="update_request"),
            InlineKeyboardButton("🗑️ Delete", callback_data="delete_request")
        ]
    ]
    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(keyboard), rows

def build_row_selection_keyboard(rows, action_type):
    """Build keyboard with compact row number buttons"""
    keyboard = []
    current_row = []

    for i, r in enumerate(rows):
        button_data = f"upd|{r['row']}" if action_type == "update" else f"del|{r['row']}"
        button = InlineKeyboardButton(str(r['row']), callback_data=button_data)
        current_row.append(button)

        if len(current_row) == 4 or i == len(rows) - 1:
            keyboard.append(current_row)
            current_row = []

    action_text = "Update" if action_type == "update" else "Delete"
    keyboard.append([InlineKeyboardButton(f"👆 Click row number to {action_text.lower()}", callback_data="noop")])
    keyboard.append([InlineKeyboardButton("← Back to List", callback_data="back_to_list")])
    return InlineKeyboardMarkup(keyboard)

def build_filter_type_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📅 Date", callback_data="filter_type_date"),
            InlineKeyboardButton("📂 Category", callback_data="filter_type_category")
        ],
        [
            InlineKeyboardButton("💰 Amount", callback_data="filter_type_amount"),
            InlineKeyboardButton("📝 Description", callback_data="filter_type_reason")
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="filter_cancel")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_date_filter_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📆 Today", callback_data="date_filter_today"),
            InlineKeyboardButton("📆 Yesterday", callback_data="date_filter_yesterday")
        ],
        [
            InlineKeyboardButton("📅 Last Week", callback_data="date_filter_lastweek"),
            InlineKeyboardButton("📅 Last Month", callback_data="date_filter_lastmonth")
        ],
        [
            InlineKeyboardButton("🗓️ This Month", callback_data="date_filter_thismonth"),
            InlineKeyboardButton("📊 Custom Date", callback_data="date_filter_custom")
        ],
        [
            InlineKeyboardButton("📈 Date Range", callback_data="date_filter_range"),
            InlineKeyboardButton("← Back", callback_data="filter_back_main")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_amount_filter_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("💯 Exactly", callback_data="amount_filter_exact"),
            InlineKeyboardButton("⬆️ Greater Than", callback_data="amount_filter_gt")
        ],
        [
            InlineKeyboardButton("⬇️ Less Than", callback_data="amount_filter_lt"),
            InlineKeyboardButton("← Back", callback_data="filter_back_main")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_category_filter_keyboard(chat_id):
    try:
        categories = get_all_categories()
        # Buttons carry an index; the names are kept in the chat's filter state, because
        # Telegram caps callback_data at 64 bytes.
        interactive_filter_state.setdefault(chat_id, {'filters': {}})['categories'] = categories
        keyboard = []

        if not categories:
            keyboard.append([InlineKeyboardButton("📭 No Categories Found", callback_data="noop")])
        else:
            buttons = [InlineKeyboardButton(f"🏷️ {name[:40]}", callback_data=f"cat_filter_i{i}")
                       for i, name in enumerate(categories)]
            for i in range(0, len(buttons), 2):
                keyboard.append(buttons[i:i + 2])

        keyboard.extend([
            [InlineKeyboardButton("✏️ Custom Category", callback_data="cat_filter_custom")],
            [InlineKeyboardButton("← Back", callback_data="filter_back_main")]
        ])
        return InlineKeyboardMarkup(keyboard)
    except:
        keyboard = [
            [InlineKeyboardButton("⚠️ Error Loading", callback_data="noop")],
            [InlineKeyboardButton("← Back", callback_data="filter_back_main")]
        ]
        return InlineKeyboardMarkup(keyboard)

def build_count_selection_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("5️⃣ 5 Results", callback_data="count_5"),
            InlineKeyboardButton("🔟 10 Results", callback_data="count_10")
        ],
        [
            InlineKeyboardButton("2️⃣0️⃣ 20 Results", callback_data="count_20"),
            InlineKeyboardButton("5️⃣0️⃣ 50 Results", callback_data="count_50")
        ],
        [
            InlineKeyboardButton("← Back", callback_data="filter_back_main")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def expense_saved_message(title, row, amount, reason, category, notes, user, verb="Added"):
    category_text = f"🏷️ {md(category)}" if category else "🏷️ uncategorized"
    notes_text = f" • {md(notes)}" if notes else ""
    row_text = f"**Row {row}:** " if row else ""
    return (f"{title}\n\n{row_text}`₹{amount}` {md(reason) or 'expense'}\n"
            f"{category_text}{notes_text}\n\n{verb} by: {md(user)}")


# ---------------- bot handlers ----------------
async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler; stops updates from users who are not allowed."""
    user = update.effective_user
    if user and user.id in ALLOWED_USER_IDS:
        return
    if user:
        logger.warning("Refused update from user id %s", user.id)
        text = (f"🔒 This bot is private.\n\nYour Telegram user id is `{user.id}`. "
                "If this is your bot, add it to `ALLOWED_USER_IDS` and restart it.")
        if update.callback_query:
            await update.callback_query.answer("This bot is private.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(text, parse_mode='Markdown')
    raise ApplicationHandlerStop


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = " ".join(context.args) if context.args else "this month"
    start, end = parse_date_input(period)
    if not start:
        await update.message.reply_text(
            "❌ **Unknown period.**\n\nTry `/summary`, `/summary last month`, `/summary August 2025` "
            "or `/summary 01-08-2025 to 15-08-2025`.", parse_mode='Markdown')
        return
    try:
        entries = filter_entries_by_criteria(read_entries(), {'date_start': start, 'date_end': end})
    except Exception as e:
        logger.exception("summary failed")
        await update.message.reply_text(f"❌ **Could not read the sheet.**\n\n`{md(e)}`", parse_mode='Markdown')
        return
    label = start if start == end else f"{start} to {end}"
    if not entries:
        await update.message.reply_text(f"📭 **No expenses for {label}.**", parse_mode='Markdown')
        return
    total, by_category = summarize(entries)
    lines = [f"📊 **Summary: {label}**", "",
             f"**Total:** `₹{format_amount(total)}` across {len(entries)} expenses", ""]
    for category, amount, count in by_category:
        share = (amount / total * 100) if total else 0
        lines.append(f"🏷️ {md(category)}: `₹{format_amount(amount)}` ({count}, {share:.0f}%)")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')



HELP_TEXT = """
🎯 **Expense Tracker Bot** 🎯

📝 **Quick Add:** `120 chai #food @morning tea`

🔧 **Basic Commands:**
• `/add` → Add new expense
• `/list [n]` → Show last n entries (default: 10)
• `/sheet` → Get Google Sheets link
• `/update <row>` → Update specific row
• `/delete <row>` → Delete specific row
• `/list_filter` → Filter expenses
• `/summary [period]` → Totals by category (default: this month)
• `/help` → Show this help

📋 **Format:** `<amount> <description> #[category] @[notes (optional)]`

🎮 **Interactive Mode:**
• Use `/list` for entry manipulation
• Use `/list_filter` for advanced filtering

✨ Everything is saved to your Google Sheet (`/sheet`).
"""


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = """
🎉 **Welcome to Expense Tracker Bot!** 🎉

💡 **Quick Start:**
• Send: `120 coffee #food` to add an expense
• Use `/list` to see your recent expenses
• Use `/help` for detailed instructions

Ready to track your expenses? Let's go! 🚀
    """
    await update.message.reply_text(welcome_msg.strip(), parse_mode='Markdown')


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT.strip(), parse_mode='Markdown')


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = " ".join(context.args) if context.args else ""
    if not msg_text:
        help_msg = """
📝 **Add Expense Command**

**Usage:** `/add <amount> <description> [#category] [@notes]`

**Examples:**
• `/add 120 coffee #food`
• `/add 50 bus fare #transport @to work`
• `/add 300 groceries #shopping @weekly`

💡 **Tip:** You can also just send a message without the `/add` command!
        """
        await update.message.reply_text(help_msg.strip(), parse_mode='Markdown')
        return
    amount, reason, category, notes = parse_message(msg_text)
    if not amount:
        error_msg = """
❌ **Amount not found!**

Please start with a number.

**Examples:**
• `/add 120 coffee #food`
• `/add 50.5 bus fare #transport`

💡 The amount should be the first number in your message.
        """
        await update.message.reply_text(error_msg.strip(), parse_mode='Markdown')
        return
    user = update.effective_user.username or update.effective_user.first_name
    try:
        row = append_entry(amount, reason, category, notes, user)

        # Format success message
        success_msg = expense_saved_message("✅ **Expense added!**", row, amount, reason, category, notes, user)
        await update.message.reply_text(success_msg.strip(), parse_mode='Markdown')
    except Exception as e:
        logger.exception("append failed")
        await update.message.reply_text(f"❌ **Failed to add expense!**\n\nGoogle Sheets error: `{str(e)}`\n\nPlease try again.", parse_mode='Markdown')


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get count from args, default to 10
        n = 10
        if context.args:
            try:
                n = int(context.args[0])
                if n > 50:  # Limit to prevent spam
                    n = 50
            except ValueError:
                pass

        result = build_list_message_and_keyboard(n)
        chat_id = update.effective_chat.id

        if result[1] is None:  # No entries case
            await update.message.reply_text(result[0], parse_mode='Markdown')
            return

        msg, keyboard, entries = result
        # Store current list data for this chat to fix "Session expired" issue
        current_list_data[chat_id] = (msg, entries)
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=keyboard)
    except Exception as e:
        logger.exception("list failed")
        await update.message.reply_text(f"❌ **Failed to list expenses!**\n\nGoogle Sheets error: `{str(e)}`\n\nPlease try again.", parse_mode='Markdown')


async def sheet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return the Google Sheets URL for direct access"""
    sheets_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_KEY}"

    msg = f"""
📊 **Your Expense Tracker Sheet**

🔗 **Direct Link:**
{sheets_url}

💡 **Quick Access:** Tap the link above to open your expense tracker in Google Sheets!
    """

    await update.message.reply_text(msg.strip(), parse_mode='Markdown')


async def list_categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        categories = get_all_categories()
        if not categories:
            msg = """
📂 **No categories found!**

You haven't added any expenses with categories yet.

**Add categories by using # in your expenses:**
• `120 coffee #food`
• `50 bus #transport`
• `300 groceries #shopping`
            """
            await update.message.reply_text(msg.strip(), parse_mode='Markdown')
            return

        # Format categories nicely
        category_list = []
        for i, cat in enumerate(categories, 1):
            category_list.append(f"{i}. `{cat}`")

        msg = f"""
📂 **Available Categories ({len(categories)} total):**

{chr(10).join(category_list)}

💡 **Filter by category:**
• `/list_filter category food`
• `/list_filter category transport`

🔍 **View all filtering options:** `/help_filters`
        """
        await update.message.reply_text(msg.strip(), parse_mode='Markdown')

    except Exception as e:
        logger.exception("Failed to get categories")
        await update.message.reply_text("❌ **Error getting categories!**\n\nPlease try again later.", parse_mode='Markdown')


async def list_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interactive filter command with beautiful UI"""
    if context.args and len(context.args) >= 2:
        # Keep old functionality for backwards compatibility
        await legacy_list_filter(update, context)
        return

    # New interactive filter interface
    chat_id = update.effective_chat.id

    # Initialize filter state
    interactive_filter_state[chat_id] = {
        'filters': {},
        'count': 10,
        'step': 'select_type'
    }

    welcome_msg = """
🔍 **Interactive Filter**

Choose what you want to filter by:
    """

    keyboard = build_filter_type_keyboard()
    await update.message.reply_text(welcome_msg.strip(), reply_markup=keyboard, parse_mode='Markdown')


async def legacy_list_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy text-based filter for backwards compatibility"""
    filter_type = context.args[0].lower()

    # For date filters, we need to handle multi-word arguments and count parameters carefully
    if filter_type == "date":
        # Check if the last argument is a reasonable count (small number, not a year)
        last_arg_is_count = False
        count = 10  # Default count

        if len(context.args) >= 3:
            try:
                potential_count = int(context.args[-1])
                # Only consider it a count if it's reasonable (not a year like 2025)
                if 1 <= potential_count <= 200:
                    count = potential_count
                    last_arg_is_count = True
                    if count > 50:  # Limit to prevent spam
                        count = 50
            except ValueError:
                pass

        # Extract filter value (everything between filter_type and count if present)
        if last_arg_is_count:
            filter_value = " ".join(context.args[1:-1])
        else:
            filter_value = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    else:
        # For non-date filters, use simpler logic
        filter_value = context.args[1] if len(context.args) > 1 else ""
        count = 10  # Default count for filtered results

        if len(context.args) > 2:
            try:
                count = int(context.args[2])
                if count > 50:  # Limit to prevent spam
                    count = 50
            except ValueError:
                pass

    # Parse filter criteria
    filters = {}
    filter_info = ""

    try:
        if filter_type == "category":
            filters['category'] = filter_value
            filter_info = f"\n🏷️ **Category:** `{filter_value}`"

        elif filter_type == "date":
            # Use new flexible date parsing
            start_date, end_date = parse_date_input(filter_value)
            if not start_date or not end_date:
                await update.message.reply_text("❌ **Invalid date format!**\n\nSupported formats:\n• `dd-mm-yyyy` or `dd/mm/yyyy`\n• `mm-yyyy` or `mm/yyyy`\n• `August 2025` or `Aug 25`\n• `today`, `yesterday`, `last week`, `last month`\n• `01-01-2025 to 31-01-2025`", parse_mode='Markdown')
                return

            if start_date == end_date:
                filters['date_start'] = start_date
                filters['date_end'] = end_date
                filter_info = f"\n📅 **Date:** `{start_date}`"
            else:
                filters['date_start'] = start_date
                filters['date_end'] = end_date
                filter_info = f"\n📅 **Date Range:** `{start_date}` to `{end_date}`"

        elif filter_type == "amount":
            float(filter_value)  # Validate number
            filters['amount'] = filter_value
            filters['amount_op'] = 'eq'
            filter_info = f"\n💰 **Amount:** `₹{filter_value}`"

        elif filter_type == "amount_gt":
            float(filter_value)  # Validate number
            filters['amount'] = filter_value
            filters['amount_op'] = 'gt'
            filter_info = f"\n💰 **Amount >:** `₹{filter_value}`"

        elif filter_type == "amount_lt":
            float(filter_value)  # Validate number
            filters['amount'] = filter_value
            filters['amount_op'] = 'lt'
            filter_info = f"\n💰 **Amount <:** `₹{filter_value}`"

        elif filter_type == "reason":
            filters['reason'] = filter_value
            filter_info = f"\n📝 **Description:** `{filter_value}`"

        else:
            await update.message.reply_text(f"❌ **Unknown filter type:** `{filter_type}`\n\nUse `/list_filter` to see available options.", parse_mode='Markdown')
            return

        # Get filtered entries
        entries = get_entries_with_filter(count, filters)
        text = build_filtered_list_message(entries, filter_info)

        await update.message.reply_text(text, parse_mode='Markdown')

    except ValueError as e:
        if "time data" in str(e):
            await update.message.reply_text("❌ **Invalid date format!**\n\nPlease use `YYYY-MM-DD` format.\n**Example:** `2025-08-10`", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ **Invalid value for {filter_type}!**\n\nPlease check the format and try again.", parse_mode='Markdown')
    except Exception as e:
        logger.exception("Failed to filter entries")
        await update.message.reply_text("❌ **Filter failed!**\n\nPlease try again.", parse_mode='Markdown')


async def help_filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_msg = """
🔍 **Advanced Filtering Guide**

**📋 Basic Usage:**
`/list_filter <type> <value> [count]`

**🎯 Filter Types:**

**📂 Category Filters:**
• `/list_filter category food` → Show food expenses
• `/list_filter category misc` → Show miscellaneous expenses
• `/list_categories` → List all available categories

**📅 Date Filters (Flexible Formats):**

**🗓️ Single Dates:**
• `/list_filter date 10-08-2025` → DD-MM-YYYY format
• `/list_filter date 10/08/2025` → DD/MM/YYYY format
• `/list_filter date 2025-08-10` → YYYY-MM-DD format

**📆 Month/Year:**
• `/list_filter date 08-2025` → MM-YYYY format
• `/list_filter date 08/25` → MM-YY format
• `/list_filter date August 2025` → Month name + year
• `/list_filter date Aug 25` → Short month + 2-digit year

**⚡ Quick Keywords:**
• `/list_filter date today` → Today's expenses
• `/list_filter date yesterday` → Yesterday's expenses
• `/list_filter date last week` → Previous week
• `/list_filter date last month` → Previous month

**📊 Date Ranges:**
• `/list_filter date 01-08-2025 to 31-08-2025` → Full month
• `/list_filter date 10/08/2025 to 15/08/2025` → Specific range
• `/list_filter date August 2025 to September 2025` → Month range

**💰 Amount Filters:**
• `/list_filter amount 120` → Exactly ₹120
• `/list_filter amount_gt 100` → More than ₹100
• `/list_filter amount_lt 50` → Less than ₹50

**📝 Description Filters:**
• `/list_filter reason coffee` → Contains "coffee"
• `/list_filter reason lunch` → Contains "lunch"

**� Examples with Count:**
• `/list_filter category food 15` → Last 15 food expenses
• `/list_filter amount_gt 200 5` → Last 5 expenses > ₹200
• `/list_filter date today 20` → Today's expenses (max 20)

**💡 Pro Tips:**
• All separators (-/) are interchangeable
• Month names work in English (full/short)
• Date ranges use "to" keyword
• Filters are case-insensitive
• Default count: 10, Maximum: 50
    """
    await update.message.reply_text(help_msg.strip(), parse_mode='Markdown')


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /update <row_number> <amount> <reason> [#category] [@notes]")
        return
    try:
        row_num = int(context.args[0])
    except:
        await update.message.reply_text("Row number must be an integer as shown by /list (sheet row number).")
        return
    msg_text = " ".join(context.args[1:])
    amount, reason, category, notes = parse_message(msg_text)
    if not amount:
        await update.message.reply_text("Couldn't find amount in provided data.")
        return
    user = update.effective_user.username or update.effective_user.first_name
    try:
        update_row_exact(row_num, amount, reason, category, notes, user)
        await update.message.reply_text(f"Updated row {row_num}.")
    except Exception as e:
        logger.exception("update failed")
        await update.message.reply_text(f"Failed to update row {row_num}: {e}")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <row_number>")
        return
    try:
        row_num = int(context.args[0])
    except:
        await update.message.reply_text("Row number must be an integer as shown by /list (sheet row number).")
        return
    # confirmation buttons
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes, delete", callback_data=f"delconf|{row_num}"),
         InlineKeyboardButton("Cancel", callback_data=f"delcan|{row_num}")]
    ])
    await update.message.reply_text(f"Confirm delete row {row_num}?", reply_markup=kb)


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    # Handle interactive filter inputs
    if chat_id in interactive_input_state:
        await handle_interactive_input(update, context, text)
        return

    # If user is in pending_update flow, treat this message as updated content
    if chat_id in pending_update:
        row_to_update = pending_update.pop(chat_id)
        amount, reason, category, notes = parse_message(text)
        if not amount:
            await update.message.reply_text("❌ **Invalid update data!**\n\nCouldn't find amount. Please start with a number.\n\n**Example:** `150 dinner #food`", parse_mode='Markdown')
            return
        user = update.effective_user.username or update.effective_user.first_name
        try:
            update_row_exact(row_to_update, amount,
                             reason, category, notes, user)

            success_msg = expense_saved_message("✅ **Successfully updated!**", row_to_update, amount,
                                                reason, category, notes, user, verb="Updated")
            await update.message.reply_text(success_msg.strip(), parse_mode='Markdown')

            # Clear the stored list data since it's now outdated
            if chat_id in current_list_data:
                del current_list_data[chat_id]
        except Exception as e:
            logger.exception("pending update failed")
            await update.message.reply_text(f"❌ **Update failed!**\n\nError: `{str(e)}`\n\nPlease try again.", parse_mode='Markdown')
        return

    # normal add-by-message flow - show confirmation dialog
    amount, reason, category, notes = parse_message(text)
    if not amount:
        await update.message.reply_text("💡 **Quick Add Help**\n\nSend messages like:\n• `120 coffee #food`\n• `50 bus fare #transport @to work`\n\nOr use `/add` command for help.", parse_mode='Markdown')
        return

    user = update.effective_user.username or update.effective_user.first_name

    # Create confirmation dialog with all details
    category_text = f"🏷️ {md(category)}" if category else "🏷️ uncategorized"
    notes_text = f"\n📝 Notes: {md(notes)}" if notes else ""

    confirmation_msg = f"""
💰 **Confirm Expense Addition**

**Amount:** `₹{amount}`
**Description:** {md(reason) or 'expense'}
{category_text}{notes_text}
**Added by:** {md(user)}

Do you want to add this expense?
    """

    # Keep the expense server-side; callback_data is capped at 64 bytes and
    # a '|' in the description would break any delimiter-based encoding.
    pending_add[chat_id] = (amount, reason, category, notes, user)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Add", callback_data="addconf"),
         InlineKeyboardButton("❌ Cancel", callback_data="addcan")]
    ])

    await update.message.reply_text(confirmation_msg.strip(), reply_markup=keyboard, parse_mode='Markdown')


async def handle_interactive_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle user input during interactive filtering"""
    chat_id = update.effective_chat.id
    input_state = interactive_input_state.get(chat_id)

    if not input_state:
        return

    input_type = input_state['type']

    try:
        if input_type == 'custom_date':
            # Parse custom date input
            start_date, end_date = parse_date_input(text)
            if not start_date or not end_date:
                await update.message.reply_text("❌ **Invalid date format!**\n\nTry formats like:\n• `10-08-2025`\n• `August 2025`\n• `01-08-2025 to 31-08-2025`\n\nPlease try again:", parse_mode='Markdown')
                return

            # Store the date filter
            filter_state = interactive_filter_state[chat_id]
            filter_state['filters']['date_start'] = start_date
            filter_state['filters']['date_end'] = end_date

            # Clear input state
            del interactive_input_state[chat_id]

            # Ask for count
            await ask_for_count(update, chat_id)

        elif input_type == 'custom_category':
            # Store category filter
            filter_state = interactive_filter_state[chat_id]
            filter_state['filters']['category'] = text.strip()

            # Clear input state
            del interactive_input_state[chat_id]

            # Ask for count
            await ask_for_count(update, chat_id)

        elif input_type == 'custom_amount_exact':
            # Validate and store exact amount
            amount = float(text.strip())
            filter_state = interactive_filter_state[chat_id]
            filter_state['filters']['amount'] = str(amount)
            filter_state['filters']['amount_op'] = 'eq'

            # Clear input state
            del interactive_input_state[chat_id]

            # Ask for count
            await ask_for_count(update, chat_id)

        elif input_type == 'custom_amount_gt':
            # Validate and store greater than amount
            amount = float(text.strip())
            filter_state = interactive_filter_state[chat_id]
            filter_state['filters']['amount'] = str(amount)
            filter_state['filters']['amount_op'] = 'gt'

            # Clear input state
            del interactive_input_state[chat_id]

            # Ask for count
            await ask_for_count(update, chat_id)

        elif input_type == 'custom_amount_lt':
            # Validate and store less than amount
            amount = float(text.strip())
            filter_state = interactive_filter_state[chat_id]
            filter_state['filters']['amount'] = str(amount)
            filter_state['filters']['amount_op'] = 'lt'

            # Clear input state
            del interactive_input_state[chat_id]

            # Ask for count
            await ask_for_count(update, chat_id)

        elif input_type == 'custom_reason':
            # Store reason filter
            filter_state = interactive_filter_state[chat_id]
            filter_state['filters']['reason'] = text.strip()

            # Clear input state
            del interactive_input_state[chat_id]

            # Ask for count
            await ask_for_count(update, chat_id)

    except ValueError:
        await update.message.reply_text("❌ **Invalid input!** Please try again with a valid format.", parse_mode='Markdown')
    except Exception as e:
        logger.exception("Error handling interactive input")
        await update.message.reply_text("❌ **Something went wrong!** Please start over with `/list_filter`.", parse_mode='Markdown')
        # Clean up states
        if chat_id in interactive_input_state:
            del interactive_input_state[chat_id]
        if chat_id in interactive_filter_state:
            del interactive_filter_state[chat_id]


async def ask_for_count(update: Update, chat_id: int):
    """Ask user how many results they want"""
    msg = "🔢 **How many results do you want?**\n\nChoose from the options below:"
    keyboard = build_count_selection_keyboard()
    await update.message.reply_text(msg, reply_markup=keyboard, parse_mode='Markdown')


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""
    chat_id = query.message.chat.id

    # Handle interactive filter callbacks
    if data.startswith(('filter_type_', 'date_filter_', 'amount_filter_', 'cat_filter_', 'reason_filter_', 'count_', 'filter_back_', 'filter_cancel')) or data == 'filter_type_new':
        await handle_interactive_filter_callback(query, data, chat_id)
        return

    # Handle existing callbacks
    if data == "noop":
        # Do nothing for instruction buttons
        return

    if data == "update_request":
        # User clicked Update button, show row selection buttons
        if chat_id in current_list_data:
            text, rows = current_list_data[chat_id]
            keyboard = build_row_selection_keyboard(rows, "update")
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
            except Exception as e:
                logger.exception("Failed to edit message for update request")
                await query.message.reply_text("⚠️ Something went wrong. Please try `/list` again.", parse_mode='Markdown')
        else:
            await query.message.reply_text("⏰ **Session expired!**\n\nPlease run `/list` first to see available entries.", parse_mode='Markdown')

    elif data == "delete_request":
        # User clicked Delete button, show row selection buttons
        if chat_id in current_list_data:
            text, rows = current_list_data[chat_id]
            keyboard = build_row_selection_keyboard(rows, "delete")
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
            except Exception as e:
                logger.exception("Failed to edit message for delete request")
                await query.message.reply_text("⚠️ Something went wrong. Please try `/list` again.", parse_mode='Markdown')
        else:
            await query.message.reply_text("⏰ **Session expired!**\n\nPlease run `/list` first to see available entries.", parse_mode='Markdown')

    elif data == "back_to_list":
        # User clicked Back to List button
        if chat_id in current_list_data:
            text, rows = current_list_data[chat_id]
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✏️ Update", callback_data="update_request"),
                    InlineKeyboardButton(
                        "🗑️ Delete", callback_data="delete_request")
                ]
            ]
            try:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            except Exception as e:
                logger.exception("Failed to edit message for back to list")
                await query.message.reply_text("⚠️ Something went wrong. Please try `/list` again.", parse_mode='Markdown')
        else:
            await query.message.reply_text("⏰ **Session expired!**\n\nPlease run `/list` first to see available entries.", parse_mode='Markdown')

    elif data.startswith("del|"):
        _, row_s = data.split("|", 1)
        try:
            row_num = int(row_s)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, delete", callback_data=f"delconf|{row_num}"),
                 InlineKeyboardButton("❌ Cancel", callback_data=f"delcan|{row_num}")]
            ])
            await query.edit_message_text(f"🗑️ **Confirm Deletion**\n\nAre you sure you want to delete row **{row_num}**?\n\n⚠️ This action cannot be undone!", reply_markup=kb, parse_mode='Markdown')
        except ValueError:
            await query.message.reply_text("❌ **Invalid row number!**\n\nPlease try again with `/list`.", parse_mode='Markdown')
        except Exception as e:
            logger.exception("Failed to show delete confirmation")
            await query.message.reply_text("⚠️ Something went wrong. Please try `/list` again.", parse_mode='Markdown')

    elif data.startswith("delconf|"):
        _, row_s = data.split("|", 1)
        try:
            row_num = int(row_s)
            delete_row_exact(row_num)
            await query.edit_message_text(f"✅ **Successfully deleted!**\n\nRow **{row_num}** has been removed from your expenses.", parse_mode='Markdown')
            # Clear the stored list data since it's now outdated
            if chat_id in current_list_data:
                del current_list_data[chat_id]
        except ValueError:
            await query.message.reply_text(f"❌ **Error:** Invalid row number `{row_s}`", parse_mode='Markdown')
        except Exception as e:
            logger.exception("delete failed")
            await query.message.reply_text(f"❌ **Failed to delete row {row_s}**\n\nError: `{str(e)}`", parse_mode='Markdown')

    elif data.startswith("delcan|"):
        # Go back to delete row selection
        if chat_id in current_list_data:
            text, rows = current_list_data[chat_id]
            keyboard = build_row_selection_keyboard(rows, "delete")
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
            except Exception as e:
                logger.exception("Failed to edit message for delete cancel")
                await query.message.reply_text("⚠️ Something went wrong. Please try `/list` again.", parse_mode='Markdown')
        else:
            await query.message.reply_text("⏰ **Session expired!**\n\nPlease run `/list` first to see available entries.", parse_mode='Markdown')

    elif data == "addconf":
        expense = pending_add.pop(chat_id, None)
        if not expense:
            await query.edit_message_text("⏰ **This confirmation expired.** Please send the expense again.", parse_mode='Markdown')
            return
        amount, reason, category, notes, user = expense
        try:
            row = append_entry(amount, reason, category, notes, user)
            await query.edit_message_text(
                expense_saved_message("✅ **Expense added!**", row, amount, reason, category, notes, user),
                parse_mode='Markdown')
        except Exception as e:
            logger.exception("Failed to add expense from confirmation")
            await query.edit_message_text(f"❌ **Failed to add expense!**\n\nGoogle Sheets error: `{md(e)}`\n\nPlease try again.", parse_mode='Markdown')

    elif data == "addcan":
        pending_add.pop(chat_id, None)
        await query.edit_message_text("❌ **Expense addition cancelled.**", parse_mode='Markdown')

    elif data.startswith("upd|"):
        _, row_s = data.split("|", 1)
        try:
            row_num = int(row_s)
            # set pending_update for this chat
            pending_update[chat_id] = row_num
            instruction_msg = f"""
✏️ **Update Row {row_num}**

Please send the updated details as a message:

**Format:** `<amount> <description> [#category] [@notes]`

**Examples:**
• `150 dinner #food`
• `50 bus fare #transport @to work`

Just send your message now! 👇
            """
            await query.edit_message_text(instruction_msg.strip(), parse_mode='Markdown')
        except ValueError:
            await query.message.reply_text("❌ **Invalid row number!**\n\nPlease try again with `/list`.", parse_mode='Markdown')
        except Exception as e:
            logger.exception("Failed to handle update request")
            await query.message.reply_text("⚠️ Something went wrong. Please try `/list` again.", parse_mode='Markdown')

    else:
        await query.message.reply_text("❓ **Unknown action**\n\nPlease try again with `/list`.", parse_mode='Markdown')


async def handle_interactive_filter_callback(query, data: str, chat_id: int):
    """Handle all interactive filter button callbacks"""

    # Handle new filter request
    if data == 'filter_type_new':
        # Initialize new filter state
        interactive_filter_state[chat_id] = {
            'filters': {},
            'current_filter': None,
            'awaiting_input': None
        }
        text = "🔍 **Interactive Filter**\n\nWhat would you like to filter by?"
        keyboard = build_filter_type_keyboard()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')
        return

    # Handle cancellation
    if data == 'filter_cancel':
        if chat_id in interactive_filter_state:
            del interactive_filter_state[chat_id]
        if chat_id in interactive_input_state:
            del interactive_input_state[chat_id]
        await query.edit_message_text("❌ **Filter cancelled.**", parse_mode='Markdown')
        return

    # Ensure filter state exists
    if chat_id not in interactive_filter_state:
        await query.edit_message_text("❌ **Filter session expired.** Please start again with `/list_filter`.", parse_mode='Markdown')
        return

    filter_state = interactive_filter_state[chat_id]

    # Handle filter type selection
    if data.startswith('filter_type_'):
        filter_type = data.replace('filter_type_', '')
        filter_state['current_filter'] = filter_type

        if filter_type == 'date':
            text = "📅 **Date Filter Options**\n\nChoose a date range:"
            keyboard = build_date_filter_keyboard()
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

        elif filter_type == 'amount':
            text = "💰 **Amount Filter Options**\n\nChoose an amount filter:"
            keyboard = build_amount_filter_keyboard()
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

        elif filter_type == 'category':
            text = "🏷️ **Category Filter**\n\nChoose a category:"
            keyboard = build_category_filter_keyboard(chat_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')

        elif filter_type == 'reason':
            # Ask for custom reason input
            filter_state['awaiting_input'] = 'reason'
            interactive_input_state[chat_id] = {'type': 'custom_reason'}
            await query.edit_message_text("🔍 **Reason Filter**\n\nPlease type the reason you want to search for:", parse_mode='Markdown')

    # Handle date filter options
    elif data.startswith('date_filter_'):
        date_option = data.replace('date_filter_', '')

        if date_option == 'today':
            today = datetime.now().date()
            filter_state['filters']['date_start'] = today.strftime('%Y-%m-%d')
            filter_state['filters']['date_end'] = today.strftime('%Y-%m-%d')
            await ask_for_count_callback(query, chat_id)

        elif date_option == 'yesterday':
            yesterday = (datetime.now().date() - timedelta(days=1))
            filter_state['filters']['date_start'] = yesterday.strftime('%Y-%m-%d')
            filter_state['filters']['date_end'] = yesterday.strftime('%Y-%m-%d')
            await ask_for_count_callback(query, chat_id)

        elif date_option == 'lastweek':
            today = datetime.now().date()
            end_date = today - timedelta(days=today.weekday() + 1)  # Last Sunday
            start_date = end_date - timedelta(days=6)  # Monday of last week
            filter_state['filters']['date_start'] = start_date.strftime('%Y-%m-%d')
            filter_state['filters']['date_end'] = end_date.strftime('%Y-%m-%d')
            await ask_for_count_callback(query, chat_id)

        elif date_option == 'lastmonth':
            today = datetime.now().date()
            if today.month == 1:
                last_month_start = datetime(today.year - 1, 12, 1).date()
                last_month_end = datetime(today.year, 1, 1).date() - timedelta(days=1)
            else:
                last_month_start = datetime(today.year, today.month - 1, 1).date()
                last_month_end = datetime(today.year, today.month, 1).date() - timedelta(days=1)
            filter_state['filters']['date_start'] = last_month_start.strftime('%Y-%m-%d')
            filter_state['filters']['date_end'] = last_month_end.strftime('%Y-%m-%d')
            await ask_for_count_callback(query, chat_id)

        elif date_option == 'thismonth':
            today = datetime.now().date()
            start_of_month = today.replace(day=1)
            filter_state['filters']['date_start'] = start_of_month.strftime('%Y-%m-%d')
            filter_state['filters']['date_end'] = today.strftime('%Y-%m-%d')
            await ask_for_count_callback(query, chat_id)

        elif date_option == 'custom':
            filter_state['awaiting_input'] = 'date'
            interactive_input_state[chat_id] = {'type': 'custom_date'}
            await query.edit_message_text("📅 **Custom Date Range**\n\nEnter date range in formats like:\n• `10-08-2025`\n• `August 2025`\n• `01-08-2025 to 31-08-2025`", parse_mode='Markdown')

        elif date_option == 'range':
            filter_state['awaiting_input'] = 'date'
            interactive_input_state[chat_id] = {'type': 'custom_date'}
            await query.edit_message_text("📈 **Date Range**\n\nEnter date range like:\n• `01-08-2025 to 31-08-2025`\n• `August 2025 to September 2025`\n• `01/08/2025 to 15/08/2025`", parse_mode='Markdown')

    # Handle amount filter options
    elif data.startswith('amount_filter_'):
        amount_option = data.replace('amount_filter_', '')

        if amount_option == 'exact':
            filter_state['awaiting_input'] = 'amount'
            interactive_input_state[chat_id] = {'type': 'custom_amount_exact'}
            await query.edit_message_text("💯 **Exact Amount**\n\nEnter the exact amount you want to find:", parse_mode='Markdown')
        elif amount_option == 'gt':
            filter_state['awaiting_input'] = 'amount'
            interactive_input_state[chat_id] = {'type': 'custom_amount_gt'}
            await query.edit_message_text("⬆️ **Greater Than**\n\nEnter the minimum amount (expenses greater than this):", parse_mode='Markdown')
        elif amount_option == 'lt':
            filter_state['awaiting_input'] = 'amount'
            interactive_input_state[chat_id] = {'type': 'custom_amount_lt'}
            await query.edit_message_text("⬇️ **Less Than**\n\nEnter the maximum amount (expenses less than this):", parse_mode='Markdown')

    # Handle category filter selection
    elif data.startswith('cat_filter_'):
        if data == 'cat_filter_custom':
            filter_state['awaiting_input'] = 'category'
            interactive_input_state[chat_id] = {'type': 'custom_category'}
            await query.edit_message_text("🏷️ **Custom Category**\n\nPlease type the category name:", parse_mode='Markdown')
        else:
            try:
                category = filter_state.get('categories', [])[int(data.replace('cat_filter_i', ''))]
            except (ValueError, IndexError):
                await query.edit_message_text("❌ **Category list expired.** Please start again with `/list_filter`.", parse_mode='Markdown')
                return
            filter_state['filters']['category'] = category
            await ask_for_count_callback(query, chat_id)

    # Handle count selection
    elif data.startswith('count_'):
        count = data.replace('count_', '')
        if count == 'all':
            count_val = 100  # Set a reasonable maximum
        else:
            count_val = int(count)

        # Apply filters and show results
        await apply_interactive_filters(query, chat_id, filter_state['filters'], count_val)

    # Handle back navigation
    elif data.startswith('filter_back_'):
        back_to = data.replace('filter_back_', '')

        if back_to == 'main':
            text = "🔍 **Interactive Filter**\n\nWhat would you like to filter by?"
            keyboard = build_filter_type_keyboard()
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='Markdown')


async def ask_for_count_callback(query, chat_id: int):
    """Ask user how many results they want (callback version)"""
    msg = "🔢 **How many results do you want?**\n\nChoose from the options below:"
    keyboard = build_count_selection_keyboard()
    await query.edit_message_text(msg, reply_markup=keyboard, parse_mode='Markdown')


async def apply_interactive_filters(query, chat_id: int, filters: dict, count: int = 10):
    """Apply the collected filters and show results"""
    try:
        logger.info(f"Applying interactive filters for chat {chat_id}: {filters}")

        # Build filter description
        filter_parts = []

        if 'date_start' in filters and 'date_end' in filters:
            if filters['date_start'] == filters['date_end']:
                filter_parts.append(f"📅 Date: {filters['date_start']}")
            else:
                filter_parts.append(f"📅 Date: {filters['date_start']} to {filters['date_end']}")

        if 'category' in filters:
            filter_parts.append(f"🏷️ Category: {md(filters['category'])}")

        if 'reason' in filters:
            filter_parts.append(f"🔍 Reason: {md(filters['reason'])}")

        if 'amount' in filters:
            if 'amount_op' in filters:
                if filters['amount_op'] == 'gt':
                    filter_parts.append(f"💰 Amount > ₹{filters['amount']}")
                elif filters['amount_op'] == 'lt':
                    filter_parts.append(f"💰 Amount < ₹{filters['amount']}")
                else:
                    filter_parts.append(f"💰 Amount = ₹{filters['amount']}")
            else:
                filter_parts.append(f"💰 Amount: ₹{filters['amount']}")

        count_text = f"📝 Count: {count}"
        filter_parts.append(count_text)

        # Get filtered entries using the existing function
        filter_dict = {}

        # Convert our filters to the format expected by the existing function
        if 'date_start' in filters:
            filter_dict['date_start'] = filters['date_start']
        if 'date_end' in filters:
            filter_dict['date_end'] = filters['date_end']
        if 'category' in filters:
            filter_dict['category'] = filters['category']
        if 'reason' in filters:
            filter_dict['reason'] = filters['reason']
        if 'amount' in filters:
            filter_dict['amount'] = filters['amount']
            if 'amount_op' in filters:
                filter_dict['amount_op'] = filters['amount_op']

        logger.info(f"Calling get_entries_with_filter with count={count}, filter_dict={filter_dict}")

        entries = get_entries_with_filter(n=count, filters=filter_dict)
        logger.info(f"Got {len(entries)} entries from filter")

        if not entries:
            result_text = f"""
✨ **Filter Applied**

{chr(10).join(filter_parts)}

❌ **No matching entries found!**

Try adjusting your filters or check if you have expenses matching the criteria.
            """
            await query.edit_message_text(result_text.strip(), parse_mode='Markdown')
        else:
            # Store results for potential updates in the format expected by existing handlers
            current_list_data[chat_id] = ("Filtered Results", entries)

            # Format results using existing function
            filter_info = f"\n{chr(10).join(filter_parts)}"
            result_text = build_filtered_list_message(entries, filter_info)

            # Create keyboard for actions
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📝 Update Entry", callback_data="update_request"),
                 InlineKeyboardButton("🔄 New Filter", callback_data="filter_type_new")]
            ])

            await query.edit_message_text(result_text, reply_markup=keyboard, parse_mode='Markdown')

        # Clean up filter state
        if chat_id in interactive_filter_state:
            del interactive_filter_state[chat_id]

    except Exception as e:
        logger.exception("Error applying interactive filters")
        error_msg = f"❌ **Error applying filters!**\n\nError: `{str(e)}`\n\nPlease try again with `/list_filter`."
        await query.edit_message_text(error_msg.strip(), parse_mode='Markdown')


# ---------------- main ----------------
def main():
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS is empty: every user will be refused. "
                       "Message the bot once to learn your user id, then set it.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Group -1 runs before all other handlers.
    app.add_handler(TypeHandler(Update, access_guard), group=-1)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("sheet", sheet_cmd))
    app.add_handler(CommandHandler("list_categories", list_categories_cmd))
    app.add_handler(CommandHandler("list_filter", list_filter_cmd))
    app.add_handler(CommandHandler("help_filters", help_filters_cmd))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("summary", summary_cmd))

    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot starting polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
