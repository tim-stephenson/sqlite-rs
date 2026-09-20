"""Tests for the columnar Arrow results.

Results come back as one array per column, each as long as the number of rows,
exported over the Arrow PyCapsule interface. SQLite types values per row rather
than per column, so each column is accumulated optimistically and promoted
along NULL < INTEGER < REAL < TEXT < BLOB when a wider value turns up.
"""

import arrow_util as au
import pytest
import sqlite_rs
import sqlite_rs.sqlite3


def _untyped_column(values: list[object]) -> tuple[str, list[object]]:
    """Insert `values` into a column with no declared type, and read it back.

    Without a declared type SQLite stores each value's own storage class, which
    is what makes a mixed-type column -- and so promotion -- possible at all.
    """
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE m (v)")
    _ = conn.executemany("INSERT INTO m VALUES (?)", [(v,) for v in values])
    [column] = sqlite_rs.execute_and_fetch_all(conn, "SELECT v FROM m")
    return au.dtype(column), au.values(column)


def test_results_are_one_array_per_column() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    _ = conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y"), (3, "z")])

    columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT a, b FROM t")

    assert len(columns) == 2  # noqa: PLR2004
    assert all(len(c) == 3 for c in columns)  # noqa: PLR2004
    assert au.columns(columns) == [[1, 2, 3], ["x", "y", "z"]]


def test_columns_carry_their_names() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    _ = conn.execute("INSERT INTO t VALUES (1, 'x')")

    columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT a, b AS renamed FROM t")

    assert [au.name(c) for c in columns] == ["a", "renamed"]


def test_arrays_export_over_the_pycapsule_interface() -> None:
    # The whole point of returning Arrow: pyarrow, polars and duckdb consume
    # these without sqlite_rs depending on any of them.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    [column] = sqlite_rs.execute_and_fetch_all(conn, "SELECT 1")

    schema_capsule = column.__arrow_c_schema__()
    schema, array = column.__arrow_c_array__()

    assert "arrow_schema" in repr(schema_capsule)
    assert "arrow_schema" in repr(schema)
    assert "arrow_array" in repr(array)


@pytest.mark.parametrize(
    ("sql", "expected_type", "expected"),
    [
        ("SELECT 1", "int64", [1]),
        ("SELECT 1.5", "float64", [1.5]),
        ("SELECT 'x'", "utf8", ["x"]),
        ("SELECT x'00ff'", "binary", [b"\x00\xff"]),
        ("SELECT NULL", "null", [None]),
    ],
)
def test_each_storage_class_maps_to_an_arrow_type(
    sql: str, expected_type: str, expected: list[object]
) -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")

    [column] = sqlite_rs.execute_and_fetch_all(conn, sql)

    assert au.dtype(column) == expected_type
    assert au.values(column) == expected


@pytest.mark.parametrize(
    ("values", "expected_type", "expected"),
    [
        # Nothing to infer from: a column of only NULLs stays Arrow null.
        ([None, None], "null", [None] * 2),
        # NULL never promotes anything; it only contributes a null slot.
        ([None, 3], "int64", [None, 3]),
        ([3, None], "int64", [3, None]),
        # Widening, in order.
        ([1, 2.5], "float64", [1.0, 2.5]),
        ([1, "x"], "utf8", ["1", "x"]),
        ([1.5, "x"], "utf8", ["1.5", "x"]),
        (["x", b"\xff"], "binary", [b"x", b"\xff"]),
        ([1, b"\xff"], "binary", [b"1", b"\xff"]),
        # An integral REAL keeps its ".0", as CAST(2.0 AS TEXT) does.
        ([2.0, "x"], "utf8", ["2.0", "x"]),
    ],
)
def test_mixed_columns_promote_left_to_right(
    values: list[object], expected_type: str, expected: list[object]
) -> None:
    assert _untyped_column(values) == (expected_type, expected)


def test_promotion_is_one_way() -> None:
    # A narrower value arriving later widens to the type already chosen; it
    # does not drag the column back down.
    assert _untyped_column([2.5, 1]) == ("float64", [2.5, 1.0])
    assert _untyped_column(["x", 1]) == ("utf8", ["x", "1"])


def test_promotion_is_applied_step_by_step() -> None:
    # 1 is widened to REAL when 2.5 arrives, so by the time BLOB forces the
    # final type it reads "1.0" rather than the "1" a direct INTEGER -> BLOB
    # promotion would have produced.
    assert _untyped_column([1, 2.5, b"\xff"]) == (
        "binary",
        [b"1.0", b"2.5", b"\xff"],
    )


def test_cursor_results_promote_identically() -> None:
    # fetch_all reads the statement through a different path than
    # execute_and_fetch_all, so it gets the same coverage.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE m (v)")
    _ = conn.executemany("INSERT INTO m VALUES (?)", [(1,), (2.5,), ("x",)])

    [via_cursor] = sqlite_rs.fetch_all(conn.execute("SELECT v FROM m"))
    [via_connection] = sqlite_rs.execute_and_fetch_all(conn, "SELECT v FROM m")

    assert au.dtype(via_cursor) == au.dtype(via_connection)
    assert au.values(via_cursor) == au.values(via_connection)
    # 1 is widened to REAL by 2.5 before TEXT forces the final type.
    assert au.values(via_cursor) == ["1.0", "2.5", "x"]


def test_empty_result_has_columns_but_no_rows() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")

    columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT a, b FROM t")

    assert [au.name(c) for c in columns] == ["a", "b"]
    assert all(len(c) == 0 for c in columns)


def test_statement_with_no_result_columns_returns_nothing() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER)")

    assert sqlite_rs.execute_and_fetch_all(conn, "INSERT INTO t VALUES (1)") == []
    assert au.rows(sqlite_rs.execute_and_fetch_all(conn, "SELECT a FROM t")) == [[1]]
