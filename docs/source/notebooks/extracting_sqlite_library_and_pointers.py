import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium")


@app.cell
def _():
    import ctypes
    import sqlite3
    import sys

    import sqlite_rs
    import sqlite_rs._sqlite3

    return ctypes, sqlite3, sqlite_rs, sys


@app.cell
def _(sys):
    sys.executable
    return


@app.cell
def _(sys):
    sys.platform
    return


@app.cell
def _(sqlite_rs):
    sqlite_rs.__all__
    return


@app.cell
def _(sqlite_rs):
    # sqlite_rs.sqlite3 mirrors the stdlib sqlite3 package's own layout: a
    # DB-API 2.0 wrapper (sqlite3.dbapi2) around a C extension
    # (sqlite3._sqlite3), both vendored unmodified from CPython and both
    # importable at these exact dotted paths.
    import sqlite_rs.sqlite3
    import sqlite_rs.sqlite3._sqlite3
    import sqlite_rs.sqlite3.dbapi2

    (sqlite_rs.sqlite3, sqlite_rs.sqlite3.dbapi2, sqlite_rs.sqlite3._sqlite3)  # noqa: SLF001
    return


@app.cell
def _(sqlite3, sqlite_rs):
    # sqlite_rs.sqlite3.connect() is a from-scratch build of CPython's own
    # sqlite3 module, dynamically linked against sqlite_rs's own bundled
    # libsqlite3 -- so its version can (and does) differ from the system
    # Python's.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    (sqlite3.sqlite_version, conn.execute("select sqlite_version()").fetchone()[0])
    return (conn,)


@app.cell
def _(conn):
    conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'hello from python')")
    conn.commit()
    conn.execute("SELECT * FROM t").fetchall()
    return


@app.cell
def _(conn, sqlite_rs):
    # Same live connection, read from the Rust side.
    sqlite_rs.query_via_rust(conn, "SELECT * FROM t")
    return


@app.cell
def _(conn, sqlite_rs):
    # Write from the Rust side, then read back through the Python clone
    # module -- both sides are operating on the exact same sqlite3*.
    sqlite_rs.query_via_rust(conn, "INSERT INTO t VALUES (2, 'hello from rust')")
    conn.execute("SELECT * FROM t ORDER BY a").fetchall()
    return


@app.cell
def _(conn, sqlite_rs):
    # The raw sqlite3* backing `conn`, as a ctypes.c_void_p -- for handing
    # to a completely unrelated FFI caller.
    db_ptr = sqlite_rs.get_raw_db_ptr(conn)
    db_ptr
    return (db_ptr,)


@app.cell
def _(ctypes, sqlite_rs):
    # Load the bundled libsqlite3 directly -- the same shared library file
    # sqlite_rs.sqlite3.connect() and query_via_rust are both already using
    # -- via plain ctypes, no sqlite_rs API involved in this cell at all.
    libsqlite3 = ctypes.CDLL(sqlite_rs.LIBSQLITE3_PATH)
    libsqlite3.sqlite3_exec.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    libsqlite3.sqlite3_exec.restype = ctypes.c_int
    libsqlite3
    return (libsqlite3,)


@app.cell
def _(conn, db_ptr, libsqlite3):
    # Write purely through ctypes, straight into the connection sqlite_rs
    # opened, then read it back through the Python clone module -- closing
    # the loop: Python, Rust, and raw ctypes all sharing one live connection.
    insert_sql = b"INSERT INTO t VALUES (3, 'hello from ctypes')"
    libsqlite3.sqlite3_exec(db_ptr, insert_sql, None, None, None)
    conn.execute("SELECT * FROM t ORDER BY a").fetchall()
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
