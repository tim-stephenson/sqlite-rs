"""Tests proving sqlite_rs's Python, Rust, and raw ctypes callers all interoperate.

They all dynamically link the one bundled `libsqlite3` (see vendor/sqlite/), rather than
each carrying an independent statically-linked copy, so a live connection opened by any
one of them can be driven by either of the other two.
"""

import ctypes
import sqlite3
from pathlib import Path

import arrow_util as au
import pytest
import sqlite_rs
import sqlite_rs.sqlite3

# int (*callback)(void*, int, char**, char**) -- sqlite3_exec's row callback shape.
_EXEC_CALLBACK_T = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_char_p),
    ctypes.POINTER(ctypes.c_char_p),
)


def test_execute_and_fetch_all_reads_rows_written_via_the_clone_module() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT, c REAL)")
    _ = conn.execute("INSERT INTO t VALUES (1, ?, 3.5)", ("hello",))
    conn.commit()

    columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT * FROM t")

    assert au.rows(columns) == [[1, "hello", 3.5]]
    assert [au.name(c) for c in columns] == ["a", "b", "c"]
    assert [au.dtype(c) for c in columns] == ["int64", "utf8", "float64"]


def test_execute_and_fetch_all_writes_are_visible_via_the_clone_module() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER)")

    _ = sqlite_rs.execute_and_fetch_all(conn, "INSERT INTO t VALUES (42)")

    assert conn.execute("SELECT * FROM t").fetchall() == [(42,)]


def test_execute_and_fetch_all_rejects_stdlib_sqlite3_connection() -> None:
    stdlib_conn = sqlite3.connect(":memory:")

    with pytest.raises(TypeError, match=r"sqlite_rs\.sqlite3\.connect"):
        # Passing the wrong Connection type is exactly what's under test here.
        _ = sqlite_rs.execute_and_fetch_all(
            stdlib_conn,  # pyright: ignore[reportArgumentType]
            "SELECT 1",
        )


def test_execute_and_fetch_all_reports_sql_errors() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")

    with pytest.raises(ValueError, match="no such table"):
        _ = sqlite_rs.execute_and_fetch_all(conn, "SELECT * FROM nonexistent")


def test_execute_and_fetch_all_rejects_a_second_statement() -> None:
    # sqlite3_prepare_v2 compiles the first statement and hands back the rest,
    # so accepting this would run the SELECT and silently drop the INSERT.
    conn = sqlite_rs.sqlite3.connect(":memory:")

    with pytest.raises(ValueError, match="single statement"):
        _ = sqlite_rs.execute_and_fetch_all(conn, "SELECT 1; SELECT 2")


@pytest.mark.parametrize("sql", ["", "   ", "-- just a comment", "SELECT 1;"])
def test_execute_and_fetch_all_accepts_sql_with_nothing_after_it(sql: str) -> None:
    # A trailing semicolon, whitespace or comment leaves no second statement.
    conn = sqlite_rs.sqlite3.connect(":memory:")

    _ = sqlite_rs.execute_and_fetch_all(conn, sql)


def test_rust_side_uses_the_same_sqlite_build_as_the_clone_module() -> None:
    """Guard on which libsqlite3 the Rust extension actually linked.

    libsqlite3-sys hardcodes its link name to `sqlite3`, while the bundled
    library is deliberately named libsqlite_rs_sqlite3 (see build.rs's
    shared_lib_name). If that wiring ever slips, _core links some other SQLite
    -- the system one, most likely -- and these two versions diverge.
    """
    conn = sqlite_rs.sqlite3.connect(":memory:")

    via_rust = sqlite_rs.execute_and_fetch_all(conn, "SELECT sqlite_version()")

    assert au.rows(via_rust) == [[sqlite_rs.sqlite3.sqlite_version]]


def test_execute_and_fetch_all_round_trips_a_blob() -> None:
    # rusqlite's ValueRef::Blob; the hand-written FFI this replaced returned
    # None for blob columns.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE b (data BLOB)")
    blob = b"\x00\x01\xfe\xff"
    _ = conn.execute("INSERT INTO b VALUES (?)", (blob,))

    columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT data FROM b")

    assert au.rows(columns) == [[blob]]
    assert au.dtype(columns[0]) == "binary"


def test_execute_and_fetch_all_via_raw_pointer_accepts_a_ctypes_pointer() -> None:
    # The c_void_p from get_raw_db_ptr goes straight back in, without .value.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER)")
    _ = conn.execute("INSERT INTO t VALUES (9)")

    ptr = sqlite_rs.get_raw_db_ptr(conn)
    assert ptr.value is not None  # c_void_p.value is int | None

    assert au.rows(
        sqlite_rs.execute_and_fetch_all_via_raw_pointer(ptr, "SELECT a FROM t")
    ) == [[9]]
    assert au.rows(
        sqlite_rs.execute_and_fetch_all_via_raw_pointer(ptr.value, "SELECT a FROM t")
    ) == [[9]]


def test_execute_and_fetch_all_via_raw_pointer_rejects_null() -> None:
    for null in (0, ctypes.c_void_p(), ctypes.c_void_p(0)):
        with pytest.raises(ValueError, match="db_ptr is null"):
            _ = sqlite_rs.execute_and_fetch_all_via_raw_pointer(null, "SELECT 1")


@pytest.fixture
def libsqlite3() -> ctypes.CDLL:
    """Load the bundled libsqlite3 (sqlite_rs.LIBSQLITE3_PATH) via ctypes."""
    assert sqlite_rs.LIBSQLITE3_PATH.exists()
    # str(), not the bare Path: ctypes.CDLL on Windows Python 3.11 raises
    # "argument of type 'WindowsPath' is not iterable" for a PathLike name --
    # fixed in 3.12+, but this project supports 3.11+ (confirmed via CI).
    lib = ctypes.CDLL(str(sqlite_rs.LIBSQLITE3_PATH))

    lib.sqlite3_libversion.argtypes = []
    lib.sqlite3_libversion.restype = ctypes.c_char_p
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_open.restype = ctypes.c_int
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    lib.sqlite3_close.restype = ctypes.c_int
    lib.sqlite3_exec.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        _EXEC_CALLBACK_T,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    lib.sqlite3_exec.restype = ctypes.c_int

    return lib


def _ctypes_exec_rows(
    lib: ctypes.CDLL, db: ctypes.c_void_p, sql: str
) -> list[tuple[str, ...]]:
    """Run `sql` via sqlite3_exec and collect its rows.

    sqlite3_exec always yields column values as text, regardless of storage type.
    """
    rows: list[tuple[str, ...]] = []

    def collect_row(
        _ctx: object, n: int, values: ctypes.Array[ctypes.c_char_p], _columns: object
    ) -> int:
        rows.append(tuple(values[i].decode() for i in range(n)))  # pyright: ignore[reportAny]
        return 0

    rc: int = lib.sqlite3_exec(  # pyright: ignore[reportAny]
        db, sql.encode(), _EXEC_CALLBACK_T(collect_row), None, None
    )
    assert rc == 0
    return rows


def test_bundled_libsqlite3_is_directly_usable_via_ctypes(
    libsqlite3: ctypes.CDLL, tmp_path: Path
) -> None:
    """A third, independent caller: raw ctypes, no Python sqlite wrapper involved.

    It can drive the exact same bundled SQLite build used by the clone module
    and Rust -- proving `libsqlite3` is a real, standalone, dynamically-loadable
    library, not an implementation detail baked into the other two.
    """
    clone_version = sqlite_rs.sqlite3.sqlite_version
    assert libsqlite3.sqlite3_libversion().decode() == clone_version  # pyright: ignore[reportAny]

    # Write a file-backed database via sqlite_rs's own sqlite3 clone module...
    db_path = tmp_path / "interop.db"
    conn = sqlite_rs.sqlite3.connect(str(db_path))
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    _ = conn.execute("INSERT INTO t VALUES (1, 'hello')")
    conn.commit()
    conn.close()

    # ...and read it back through ctypes calling straight into the bundled
    # libsqlite3, with no Python sqlite wrapper involved on this side at all.
    db = ctypes.c_void_p()
    assert libsqlite3.sqlite3_open(str(db_path).encode(), ctypes.byref(db)) == 0
    assert _ctypes_exec_rows(libsqlite3, db, "SELECT * FROM t") == [("1", "hello")]
    libsqlite3.sqlite3_close(db)


def test_connection_opened_via_ctypes_is_usable_via_the_sqlite_rs_api(
    libsqlite3: ctypes.CDLL, tmp_path: Path
) -> None:
    """A connection ctypes opens directly can be driven from sqlite_rs's Rust side.

    Proves the sharing isn't one-directional:
    `execute_and_fetch_all_via_raw_pointer` doesn't care that this connection
    was never touched by sqlite_rs's own Python clone module.
    """
    db_path = tmp_path / "ctypes_then_rust.db"
    db = ctypes.c_void_p()
    assert libsqlite3.sqlite3_open(str(db_path).encode(), ctypes.byref(db)) == 0

    db_ptr = db.value
    assert db_ptr is not None

    _ = sqlite_rs.execute_and_fetch_all_via_raw_pointer(
        db_ptr, "CREATE TABLE t (a INTEGER)"
    )
    _ = sqlite_rs.execute_and_fetch_all_via_raw_pointer(
        db_ptr, "INSERT INTO t VALUES (7)"
    )

    # Read back purely through ctypes on the SAME connection -- confirming the
    # writes Rust made landed on the exact connection ctypes opened, not a copy.
    assert _ctypes_exec_rows(libsqlite3, db, "SELECT * FROM t") == [("7",)]

    libsqlite3.sqlite3_close(db)


def test_sqlite_rs_connection_is_usable_via_ctypes(libsqlite3: ctypes.CDLL) -> None:
    """A connection opened w/ sqlite_rs.sqlite3.connect() driven directly by ctypes.

    The mirror image of the previous test: proves `get_raw_db_ptr` hands out a
    pointer any FFI caller can act on, not just sqlite_rs's own Rust extension.
    """
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER)")
    _ = conn.execute("INSERT INTO t VALUES (13)")
    conn.commit()

    db = sqlite_rs.get_raw_db_ptr(conn)

    # Write purely through ctypes on the SAME connection sqlite_rs opened...
    # (ctypes requires an actual null CFUNCTYPE instance here, not None, even
    # though the C API itself accepts a NULL callback.)
    no_callback = _EXEC_CALLBACK_T()
    insert_sql = b"INSERT INTO t VALUES (14)"
    rc = libsqlite3.sqlite3_exec(db, insert_sql, no_callback, None, None)  # pyright: ignore[reportAny]
    assert rc == 0

    # ...then confirm it's visible back through the Python clone module,
    # proving this is the exact same live connection, not a copy.
    assert conn.execute("SELECT * FROM t ORDER BY a").fetchall() == [(13,), (14,)]
