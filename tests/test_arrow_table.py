"""Tests for the table form of a result.

`fetch_table` and its siblings hand back the same columns as `fetch_all`, but
behind one object exporting `__arrow_c_stream__`, which is what a whole-table
consumer (polars' `DataFrame`, pyarrow's `table`) takes in a single call.

The table's own `column()` builds an arro3 object and so needs arro3 installed;
`num_rows`, `column_names` and the capsule do not, and are all these tests use.
"""

import ctypes

import arrow_util as au
import pytest
import sqlite_rs
import sqlite_rs.sqlite3


def _connection() -> sqlite_rs.sqlite3.Connection:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    _ = conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y")])
    return conn


def test_table_carries_the_column_names_and_shape() -> None:
    conn = _connection()

    table = sqlite_rs.execute_and_fetch_table(conn, "SELECT a, b AS renamed FROM t")

    assert table.column_names == ["a", "renamed"]
    assert table.shape == (2, 2)


def test_table_exports_a_stream_capsule() -> None:
    # The whole point of the table form: one object a consumer can take
    # without being handed the columns one at a time.
    conn = _connection()

    table = sqlite_rs.execute_and_fetch_table(conn, "SELECT a FROM t")

    assert "arrow_array_stream" in repr(table.__arrow_c_stream__())
    assert "arrow_schema" in repr(table.__arrow_c_schema__())


def test_table_and_list_describe_the_same_result() -> None:
    conn = _connection()
    sql = "SELECT a, b AS renamed FROM t"

    table = sqlite_rs.execute_and_fetch_table(conn, sql)
    columns = sqlite_rs.execute_and_fetch_all(conn, sql)

    assert table.column_names == [au.name(c) for c in columns]
    assert table.shape == (len(columns[0]), len(columns))


def test_fetch_table_drains_the_cursor() -> None:
    conn = _connection()
    cursor = conn.execute("SELECT a, b FROM t")

    table = sqlite_rs.fetch_table(cursor)

    assert table.num_rows == 2  # noqa: PLR2004
    assert cursor.fetchall() == []


def test_statement_with_no_result_columns_is_an_empty_table() -> None:
    conn = _connection()

    table = sqlite_rs.execute_and_fetch_table(conn, "INSERT INTO t VALUES (3, 'z')")

    assert table.shape == (0, 0)


def test_empty_result_keeps_its_columns() -> None:
    conn = _connection()

    table = sqlite_rs.execute_and_fetch_table(conn, "SELECT a, b FROM t WHERE 0")

    assert table.column_names == ["a", "b"]
    assert table.num_rows == 0


def test_raw_pointers_reach_the_same_table() -> None:
    conn = _connection()

    via_ptr = sqlite_rs.execute_and_fetch_table_via_raw_pointer(
        sqlite_rs.get_raw_db_ptr(conn), "SELECT a FROM t"
    )
    cursor = conn.execute("SELECT a FROM t")
    stmt_ptr = ctypes.c_void_p(sqlite_rs.get_raw_stmt_ptr(cursor).value)
    via_stmt = sqlite_rs.fetch_table_via_raw_pointer(stmt_ptr)

    assert via_ptr.shape == (2, 1)
    assert via_stmt.shape == (2, 1)
    # The statement was drained behind the cursor's back, so put it back in
    # order before the cursor is used (or collected) again.
    _ = sqlite_rs.fetch_all(cursor)


@pytest.mark.parametrize(
    "call",
    [
        lambda: sqlite_rs.execute_and_fetch_table(
            sqlite_rs.sqlite3.connect(":memory:"), "SELECT 1; SELECT 2"
        ),
        lambda: sqlite_rs.execute_and_fetch_table(
            sqlite_rs.sqlite3.connect(":memory:"), "SELECT * FROM nonexistent"
        ),
    ],
)
def test_table_form_reports_the_same_sql_errors(call: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        _ = call()  # pyright: ignore[reportUnknownVariableType, reportCallIssue]
