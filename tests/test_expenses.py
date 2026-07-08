import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from expenses import (filter_entries_by_criteria, md, parse_amount, parse_date_input,  # noqa: E402
                      parse_message, rows_from_values, summarize)


@pytest.mark.parametrize("text,expected", [
    ("120 coffee #food @with friends", ("120", "coffee", "food", "with friends")),
    ("coffee 120", ("120", "coffee", "misc", None)),
    ("50.5 bus fare #transport", ("50.5", "bus fare", "transport", None)),
    ("1,200 rent #home", ("1200", "rent", "home", None)),
    ("1,20,000 laptop #tech", ("120000", "laptop", "tech", None)),
    ("1,234.56 flights", ("1234.56", "flights", "misc", None)),
    ("300 groceries @weekly", ("300", "groceries", "misc", "weekly")),
    ("no amount here #x", (None, None, "x", None)),
])
def test_parse_message(text, expected):
    assert parse_message(text) == expected


def test_parse_amount_takes_the_first_number_only():
    assert parse_amount("2 coffees for 240")[0] == "2"


TODAY = date(2025, 8, 20)  # a Wednesday


@pytest.mark.parametrize("text,expected", [
    ("today", ("2025-08-20", "2025-08-20")),
    ("yesterday", ("2025-08-19", "2025-08-19")),
    ("this week", ("2025-08-18", "2025-08-20")),
    ("last week", ("2025-08-11", "2025-08-17")),
    ("this month", ("2025-08-01", "2025-08-20")),
    ("last month", ("2025-07-01", "2025-07-31")),
    ("10-08-2025", ("2025-08-10", "2025-08-10")),
    ("10/08/2025", ("2025-08-10", "2025-08-10")),
    ("2025-08-10", ("2025-08-10", "2025-08-10")),
    ("08-2025", ("2025-08-01", "2025-08-31")),
    ("02/24", ("2024-02-01", "2024-02-29")),
    ("August 2025", ("2025-08-01", "2025-08-31")),
    ("dec 24", ("2024-12-01", "2024-12-31")),
    ("01-08-2025 to 15-08-2025", ("2025-08-01", "2025-08-15")),
    ("August 2025 to September 2025", ("2025-08-01", "2025-09-30")),
    ("13-13-2025", (None, None)),
    ("15-08-2025 to 01-08-2025", (None, None)),
    ("marching band", (None, None)),
    ("", (None, None)),
])
def test_parse_date_input(text, expected):
    assert parse_date_input(text, today=TODAY) == expected


VALUES = [
    ["Timestamp", "Amount", "Reason", "Category", "Notes", "User"],
    ["2025-08-01 10:00:00", "120", "coffee", "food", "", "me"],
    ["2025-08-02 11:00:00", "1,200", "rent", "home"],          # short row, comma amount
    ["", "", "", "", "", ""],                                  # blank row is skipped
    ["2025-08-15 09:00:00", "80", "lunch", "Food", "office", "me"],
    ["2025-09-01 09:00:00", "40", "bus", "", "", "me"],        # no category
]


def test_rows_from_values_keeps_sheet_row_numbers():
    rows = rows_from_values(VALUES)
    assert [r["row"] for r in rows] == [2, 3, 5, 6]
    assert rows[1]["notes"] == "" and rows[1]["user"] == ""


def test_filters():
    rows = rows_from_values(VALUES)
    by = lambda c: [r["row"] for r in filter_entries_by_criteria(rows, c)]  # noqa: E731
    assert by({"category": "food"}) == [2, 5]
    assert by({"date_start": "2025-08-01", "date_end": "2025-08-31"}) == [2, 3, 5]
    assert by({"amount": "100", "amount_op": "gt"}) == [2, 3]
    assert by({"amount": "1200", "amount_op": "eq"}) == [3]
    assert by({"amount": "50", "amount_op": "lt"}) == [6]
    assert by({"reason": "LUN"}) == [5]


def test_summarize_groups_by_category_case_sensitively_and_handles_blanks():
    total, by_category = summarize(rows_from_values(VALUES))
    assert total == pytest.approx(1440)
    assert by_category[0] == ("home", 1200.0, 1)
    assert ("uncategorized", 40.0, 1) in by_category


def test_markdown_escaping():
    assert md("my_coffee *special* [x] `y`") == r"my\_coffee \*special\* \[x] \`y\`"
    assert md(None) == ""
