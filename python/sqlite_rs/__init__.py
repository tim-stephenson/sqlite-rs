"""sqlite_rs: a Rust reimplementation of SQLite, validated against real SQLite in-process.

Three things in this package dynamically link the exact same bundled
``libsqlite3`` (see ``vendor/sqlite/``), so a live connection can be shared
between them without serializing/reopening:

- ``sqlite_rs._sqlite3`` -- a from-scratch build of CPython's own
  ``Modules/_sqlite`` sources (vendored unmodified, see ``vendor/cpython/``),
  exposed here as :data:`connect`/:data:`Connection`.
- ``sqlite_rs._core`` -- this project's Rust extension.
- The bundled ``libsqlite3`` dynamic library itself.
"""

from sqlite_rs import _sqlite3 as _sqlite3_clone
from sqlite_rs._core import query_via_shared_connection, sum_as_string  # noqa: F401

Connection = _sqlite3_clone.Connection
connect = _sqlite3_clone.connect


def query_via_rust(connection: Connection, sql: str) -> list[list[object]]:
    """Run `sql` against `connection`'s underlying sqlite3* from the Rust side.

    `connection` must have been created by this package's :data:`connect`
    (``sqlite_rs._sqlite3``, not the stdlib ``sqlite3`` module): the Rust
    side extracts the raw ``sqlite3*`` by reading CPython's private
    Connection struct layout (see ``native/sqlite_rs_shim.c``), which is
    only guaranteed to match for connections created by this package's own
    compiled clone module, not an arbitrary stdlib `sqlite3.Connection`.
    """
    if not isinstance(connection, Connection):
        msg = (
            "query_via_rust() requires a Connection from sqlite_rs.connect() "
            "(sqlite_rs's own sqlite3 clone), not the stdlib sqlite3 module"
        )
        raise TypeError(msg)
    return query_via_shared_connection(connection, sql)
