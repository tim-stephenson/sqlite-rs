"""All three ways into sqlite_rs must report the exact same SQLite build.

sqlite_rs.sqlite3 (the CPython clone module), sqlite_rs._core (this
project's Rust extension, via query_via_rust), and raw ctypes against
sqlite_rs.LIBSQLITE3_PATH are meant to be three different callers of the
*one* bundled libsqlite3 (see test_single_libsqlite3_instance.py, which
checks that at the loaded-library level). This checks it at the SQL/C-API
level instead: sqlite3_version, sqlite3_source_id, and compile_options are
baked in at libsqlite3's own compile time, so if any one of the three
callers reported a different value for any of them, that would mean it's
actually linked against a *different* libsqlite3 -- e.g. the system's, or
one built at a different time -- despite test_single_libsqlite3_instance's
loaded-path check passing.
"""

import ctypes

import sqlite_rs
import sqlite_rs.sqlite3


def _clone_module_facts() -> tuple[str, str, set[str]]:
    # sqlite_rs.sqlite3 is a compiled clone module excluded from type
    # checking (see pyproject.toml's [tool.basedpyright] exclude list), so
    # everything read from it below is Any -- the str() calls convert it to
    # the concrete return type, but pyright still flags passing an Any
    # argument into them (reportAny fires on the *use* of Any, not just
    # assignment), hence the per-line ignores.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    row = conn.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()  # pyright: ignore[reportAny]
    assert row is not None
    version = str(row[0])  # pyright: ignore[reportAny]
    source_id = str(row[1])  # pyright: ignore[reportAny]
    options = {
        str(r[0])  # pyright: ignore[reportAny]
        for r in conn.execute("PRAGMA compile_options").fetchall()  # pyright: ignore[reportAny]
    }
    conn.close()
    return version, source_id, options


def _core_facts() -> tuple[str, str, set[str]]:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    [[version, source_id]] = sqlite_rs.query_via_rust(
        conn, "SELECT sqlite_version(), sqlite_source_id()"
    )
    options = {
        str(row[0]) for row in sqlite_rs.query_via_rust(conn, "PRAGMA compile_options")
    }
    conn.close()
    return str(version), str(source_id), options


def _ctypes_facts() -> tuple[str, str, set[str]]:
    lib = ctypes.CDLL(sqlite_rs.LIBSQLITE3_PATH)

    lib.sqlite3_libversion.argtypes = []
    lib.sqlite3_libversion.restype = ctypes.c_char_p
    lib.sqlite3_sourceid.argtypes = []
    lib.sqlite3_sourceid.restype = ctypes.c_char_p
    lib.sqlite3_compileoption_get.argtypes = [ctypes.c_int]
    lib.sqlite3_compileoption_get.restype = ctypes.c_char_p

    version = lib.sqlite3_libversion().decode()  # pyright: ignore[reportAny]
    source_id = lib.sqlite3_sourceid().decode()  # pyright: ignore[reportAny]

    # sqlite3_compileoption_get(N) walks the compile-time option list by
    # index, returning NULL once N runs past the end -- there's no count
    # function, so the only defined way to enumerate them is to keep asking
    # for the next index until it comes back NULL. Same "SQLITE_"-prefix-
    # stripped form PRAGMA compile_options uses, so these compare directly
    # against the other two callers.
    options: set[str] = set()
    index = 0
    while (option := lib.sqlite3_compileoption_get(index)) is not None:  # pyright: ignore[reportAny]
        options.add(option.decode())  # pyright: ignore[reportAny]
        index += 1

    return version, source_id, options


def test_all_three_callers_report_the_same_sqlite_build() -> None:
    clone_module = _clone_module_facts()
    core = _core_facts()
    ctypes_direct = _ctypes_facts()

    assert clone_module == core == ctypes_direct
