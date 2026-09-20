"""Tests for the cursor half of the shared-SQLite API.

`sqlite_rs.sqlite3` cursors, the Rust extension, and raw ctypes all act on one
`sqlite3_stmt*`, the same way the tests in test_connection_via_rust.py show they all
act on one `sqlite3*`. SQLite has no cursor object of its own: a DB-API cursor
is a prepared statement, so `get_raw_stmt_ptr` hands out a `sqlite3_stmt*`.
"""

import ctypes
import sqlite3
from pathlib import Path

import arrow_util as au
import pytest
import sqlite_rs
import sqlite_rs.sqlite3

_ROWS = [(1, "x"), (2, "y"), (3, "z")]

# Columns in "SELECT a, b FROM t", for the statement-shape assertion below.
_SELECTED_COLUMNS = 2


def _conn() -> sqlite_rs.sqlite3.Connection:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    _ = conn.executemany("INSERT INTO t VALUES (?, ?)", _ROWS)
    conn.commit()
    return conn


@pytest.fixture
def libsqlite3() -> ctypes.CDLL:
    """Load the bundled libsqlite3 with the statement-level entry points declared."""
    lib = ctypes.CDLL(str(sqlite_rs.LIBSQLITE3_PATH))
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_open.restype = ctypes.c_int
    lib.sqlite3_prepare_v2.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    lib.sqlite3_prepare_v2.restype = ctypes.c_int
    lib.sqlite3_finalize.argtypes = [ctypes.c_void_p]
    lib.sqlite3_finalize.restype = ctypes.c_int
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    lib.sqlite3_close.restype = ctypes.c_int
    lib.sqlite3_column_count.argtypes = [ctypes.c_void_p]
    lib.sqlite3_column_count.restype = ctypes.c_int
    lib.sqlite3_sql.argtypes = [ctypes.c_void_p]
    lib.sqlite3_sql.restype = ctypes.c_char_p
    return lib


def test_fetch_all_returns_every_row() -> None:
    # execute() has already stepped onto row 1, so a naive implementation that
    # steps before reading would silently drop it.
    cur = _conn().execute("SELECT * FROM t")

    assert au.rows(sqlite_rs.fetch_all(cur)) == [[1, "x"], [2, "y"], [3, "z"]]


def test_fetch_all_decodes_column_types_like_execute_and_fetch_all() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE v (i INTEGER, r REAL, t TEXT, n)")
    _ = conn.execute("INSERT INTO v VALUES (7, 1.5, 'txt', NULL)")

    via_cursor = sqlite_rs.fetch_all(conn.execute("SELECT * FROM v"))

    assert au.rows(via_cursor) == [[7, 1.5, "txt", None]]
    assert au.rows(via_cursor) == au.rows(
        sqlite_rs.execute_and_fetch_all(conn, "SELECT * FROM v")
    )
    assert [au.dtype(c) for c in via_cursor] == ["int64", "float64", "utf8", "null"]


def test_fetch_all_round_trips_a_blob() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE b (data BLOB)")
    _ = conn.execute("INSERT INTO b VALUES (?)", (b"\x00\xff",))

    assert au.rows(sqlite_rs.fetch_all(conn.execute("SELECT data FROM b"))) == [
        [b"\x00\xff"]
    ]


def test_fetch_all_leaves_the_cursor_safely_exhausted() -> None:
    # Regression test. Draining the statement without releasing it leaves
    # pysqlite_Cursor.statement non-NULL with no row pending, which trips
    # `assert(sqlite3_data_count(stmt) != 0)` in pysqlite_cursor_iternext and
    # aborts the interpreter -- a crash, not an exception.
    cur = _conn().execute("SELECT * FROM t")

    _ = sqlite_rs.fetch_all(cur)

    assert cur.fetchall() == []
    assert cur.fetchone() is None
    assert list(cur) == []


def test_fetch_all_resumes_from_the_cursor_position() -> None:
    # The cursor and the Rust side step one shared statement, so whatever the
    # cursor has already consumed is gone.
    cur = _conn().execute("SELECT a FROM t")

    assert cur.fetchone() == (1,)
    assert au.rows(sqlite_rs.fetch_all(cur)) == [[2], [3]]


def test_fetch_all_on_an_exhausted_cursor_reports_no_statement() -> None:
    cur = _conn().execute("SELECT * FROM t")
    assert cur.fetchall() == _ROWS

    # CPython releases the statement once its own iteration completes.
    with pytest.raises(ValueError, match="no underlying sqlite3_stmt"):
        _ = sqlite_rs.fetch_all(cur)


def test_get_raw_stmt_ptr_hands_out_the_cursors_own_statement(
    libsqlite3: ctypes.CDLL,
) -> None:
    cur = _conn().execute("SELECT a, b FROM t")

    ptr = sqlite_rs.get_raw_stmt_ptr(cur)

    assert ptr.value is not None
    # A real sqlite3_stmt*: the bundled library reads its shape and its SQL.
    assert libsqlite3.sqlite3_column_count(ptr) == _SELECTED_COLUMNS
    assert libsqlite3.sqlite3_sql(ptr) == b"SELECT a, b FROM t"
    _ = sqlite_rs.fetch_all(cur)  # release before the cursor is dropped


def test_fetch_all_via_raw_pointer_drives_a_cursors_statement() -> None:
    cur = _conn().execute("SELECT a FROM t WHERE a > 1")
    ptr = sqlite_rs.get_raw_stmt_ptr(cur).value
    assert ptr is not None

    assert au.rows(sqlite_rs.fetch_all_via_raw_pointer(ptr)) == [[2], [3]]

    _ = sqlite_rs.fetch_all(cur)  # release; see the docstring's warning


def test_fetch_all_via_raw_pointer_drives_a_ctypes_prepared_statement(
    libsqlite3: ctypes.CDLL, tmp_path: Path
) -> None:
    """The mirror of the cursor case: ctypes prepares, Rust steps.

    Nothing here has ever been touched by a Python cursor, so the statement
    arrives un-stepped -- the other branch of the same code path.
    """
    db_path = tmp_path / "raw.db"
    conn = sqlite_rs.sqlite3.connect(str(db_path))
    _ = conn.execute("CREATE TABLE t (a INTEGER)")
    _ = conn.executemany("INSERT INTO t VALUES (?)", [(10,), (20,)])
    conn.commit()
    conn.close()

    db = ctypes.c_void_p()
    assert libsqlite3.sqlite3_open(str(db_path).encode(), ctypes.byref(db)) == 0
    stmt = ctypes.c_void_p()
    assert (
        libsqlite3.sqlite3_prepare_v2(
            db, b"SELECT a FROM t ORDER BY a", -1, ctypes.byref(stmt), None
        )
        == 0
    )
    assert stmt.value is not None

    assert au.rows(sqlite_rs.fetch_all_via_raw_pointer(stmt.value)) == [[10], [20]]

    libsqlite3.sqlite3_finalize(stmt)
    libsqlite3.sqlite3_close(db)


def test_cursor_functions_reject_a_stdlib_cursor() -> None:
    stdlib_cur = sqlite3.connect(":memory:").execute("SELECT 1")

    for call in (sqlite_rs.fetch_all, sqlite_rs.get_raw_stmt_ptr):
        with pytest.raises(TypeError, match=r"sqlite_rs\.sqlite3"):
            # Passing the wrong Cursor type is exactly what's under test.
            _ = call(stdlib_cur)  # pyright: ignore[reportArgumentType]


def test_cursor_functions_reject_a_cursor_that_never_executed() -> None:
    cur = _conn().cursor()

    for call in (sqlite_rs.fetch_all, sqlite_rs.get_raw_stmt_ptr):
        with pytest.raises(ValueError, match="no underlying sqlite3_stmt"):
            _ = call(cur)


def test_fetch_all_via_raw_pointer_accepts_a_ctypes_pointer() -> None:
    # The c_void_p from get_raw_stmt_ptr goes straight back in, without .value.
    cur = _conn().execute("SELECT a FROM t WHERE a > 1")

    assert au.rows(
        sqlite_rs.fetch_all_via_raw_pointer(sqlite_rs.get_raw_stmt_ptr(cur))
    ) == [[2], [3]]

    _ = sqlite_rs.fetch_all(cur)  # release; see the docstring's warning


def test_fetch_all_via_raw_pointer_rejects_null() -> None:
    # A null address, however it is spelled.
    for null in (0, ctypes.c_void_p(), ctypes.c_void_p(0)):
        with pytest.raises(ValueError, match="stmt_ptr is null"):
            _ = sqlite_rs.fetch_all_via_raw_pointer(null)


def test_fetch_all_via_raw_pointer_rejects_a_non_pointer() -> None:
    with pytest.raises(TypeError, match="int address or a ctypes pointer"):
        # Passing the wrong type is exactly what's under test.
        _ = sqlite_rs.fetch_all_via_raw_pointer("nonsense")  # pyright: ignore[reportArgumentType]
