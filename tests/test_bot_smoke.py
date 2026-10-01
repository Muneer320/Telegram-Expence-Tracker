"""Import bot.py against a fake Google Sheet and call handlers with stub Telegram objects.

No network: gspread is replaced before import and Telegram objects are SimpleNamespaces.
"""
import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeSheet:
    def __init__(self):
        self.values = [["Timestamp", "Amount", "Reason", "Category", "Notes", "User"]]
        self.row_count = 1000
        self.calls = 0

    def get_all_values(self):
        self.calls += 1
        return [list(r) for r in self.values]

    def append_row(self, values, value_input_option=None, table_range=None):
        self.values.append(list(values))
        return {"updates": {"updatedRange": f"Sheet1!A{len(self.values)}:F{len(self.values)}"}}

    def row_values(self, n):
        return list(self.values[n - 1]) if n <= len(self.values) else []

    def update(self, rng, rows, value_input_option=None):
        n = int(rng.split(":")[0][1:])
        self.values[n - 1] = list(rows[0])

    def delete_rows(self, n):
        del self.values[n - 1]


@pytest.fixture()
def bot(monkeypatch):
    import gspread
    sheet = FakeSheet()
    client = SimpleNamespace(open_by_key=lambda key: SimpleNamespace(sheet1=sheet))
    monkeypatch.setattr(gspread, "service_account_from_dict", lambda info: client)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("SPREADSHEET_KEY", "sheet-key")
    monkeypatch.setenv("GOOGLE_SA_JSON", "{}")
    monkeypatch.setenv("ALLOWED_USER_IDS", "42, 43")
    sys.modules.pop("bot", None)
    module = importlib.import_module("bot")
    module.ws = sheet
    return module


class Replies(list):
    async def __call__(self, text, **kwargs):
        self.append((text, kwargs))


def make_update(user_id=42, text="", chat_id=7):
    replies = Replies()
    message = SimpleNamespace(text=text, reply_text=replies, chat=SimpleNamespace(id=chat_id))
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="me", first_name="Me"),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message, message=message, callback_query=None)
    return update, replies


def make_callback(data, chat_id=7, user_id=42):
    edits = Replies()

    async def answer(*a, **k):
        return None
    query = SimpleNamespace(data=data, answer=answer, edit_message_text=edits,
                            message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), reply_text=Replies()))
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id),
                             effective_chat=SimpleNamespace(id=chat_id), effective_message=None)
    return update, edits


def run(coro):
    return asyncio.run(coro)


def test_allowlist_is_parsed(bot):
    assert bot.ALLOWED_USER_IDS == {42, 43}


def test_guard_blocks_strangers_and_lets_owner_through(bot):
    from telegram.ext import ApplicationHandlerStop
    update, replies = make_update(user_id=999)
    with pytest.raises(ApplicationHandlerStop):
        run(bot.access_guard(update, None))
    assert "999" in replies[0][0]
    update, _ = make_update(user_id=42)
    assert run(bot.access_guard(update, None)) is None


def test_add_confirm_flow_with_long_description_and_pipes(bot):
    text = "1,250 dinner | dessert at a very long restaurant name that would not fit #food @team"
    update, replies = make_update(text=text)
    run(bot.message_handler(update, SimpleNamespace(args=[])))
    assert "Confirm Expense" in replies[0][0]
    buttons = replies[0][1]["reply_markup"].inline_keyboard[0]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)

    cb, edits = make_callback("addconf")
    run(bot.callback_query_handler(cb, None))
    assert "Expense added" in edits[0][0] and "Row 2" in edits[0][0]
    row = bot.ws.values[1]
    assert row[1:4] == ["1250", "dinner | dessert at a very long restaurant name that would not fit", "food"]

    cb, edits = make_callback("addconf")
    run(bot.callback_query_handler(cb, None))
    assert "expired" in edits[0][0]  # cannot be confirmed twice


def test_list_reads_the_sheet_once_and_escapes_markdown(bot):
    for i in range(30):
        bot.ws.values.append([f"2025-08-{i % 28 + 1:02d} 10:00:00", str(10 + i), "my_item", "food", "", "me"])
    bot.ws.calls = 0
    update, replies = make_update()
    run(bot.list_cmd(update, SimpleNamespace(args=["5"])))
    assert bot.ws.calls == 1
    assert r"my\_item" in replies[0][0]


def test_delete_removes_exactly_one_row(bot):
    for i in range(3):
        bot.ws.values.append(["2025-08-01 10:00:00", str(i), f"r{i}", "x", "", "me"])
    cb, edits = make_callback("delconf|3")
    run(bot.callback_query_handler(cb, None))
    assert [r[2] for r in bot.ws.values[1:]] == ["r0", "r2"]


def test_interactive_filter_respects_chosen_count(bot):
    for i in range(30):
        bot.ws.values.append(["2025-08-01 10:00:00", "5", f"item{i}", "food", "", "me"])
    bot.interactive_filter_state[7] = {"filters": {"category": "food"}}
    cb, edits = make_callback("count_20")
    run(bot.callback_query_handler(cb, None))
    assert edits[0][0].count("item") == 20


def test_category_buttons_use_short_indexes(bot):
    bot.ws.values.append(["2025-08-01 10:00:00", "5", "x", "a category name that is extremely long " * 3, "", "me"])
    keyboard = bot.build_category_filter_keyboard(7)
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cat_filter_i0" in data
    assert all(len(d.encode()) <= 64 for d in data)
    cb, edits = make_callback("cat_filter_i0")
    run(bot.callback_query_handler(cb, None))
    assert bot.interactive_filter_state[7]["filters"]["category"].startswith("a category name")


def test_summary(bot):
    bot.ws.values += [["2025-08-01 10:00:00", "100", "a", "food", "", "me"],
                      ["2025-08-02 10:00:00", "300", "b", "rent", "", "me"],
                      ["2025-08-03 10:00:00", "50", "c", "food", "", "me"]]
    update, replies = make_update()
    run(bot.summary_cmd(update, SimpleNamespace(args=["August", "2025"])))
    text = replies[0][0]
    assert "₹450" in text and "rent: `₹300`" in text and "food: `₹150`" in text
