"""A drop-in superset of Python's sqlite3 bindings, extended with Rust.

``sqlite_rs.sqlite3`` is a from-scratch build of CPython's own ``sqlite3``
module (vendored unmodified, see ``vendor/cpython/``), used exactly like it.
On top of that, ``sqlite_rs`` adds Rust functions that act on the *same* live
connection and return Arrow columns.

That sharing is the point, and it is what the bundling buys: three things here
dynamically link one and the same ``libsqlite3`` (see ``vendor/sqlite/``), so a
``sqlite3*`` or ``sqlite3_stmt*`` can be handed between them rather than
reopened.

- ``sqlite_rs.sqlite3`` -- the clone module: the DB-API 2.0 wrapper
  (``sqlite3.dbapi2``) around a C extension (``sqlite3._sqlite3``). Use it
  exactly like the stdlib ``sqlite3`` module, e.g.
  ``sqlite_rs.sqlite3.connect(...)``.
- ``sqlite_rs._core`` -- this project's Rust extension. :func:`fetch_all` and
  the rest require a connection or cursor from the clone module, not from the
  stdlib ``sqlite3``, and enforce that themselves.
- The bundled ``libsqlite3`` itself, at :data:`LIBSQLITE3_PATH`, for a
  ``ctypes`` caller that wants to drive the same connection directly.
"""

import sys
from pathlib import Path

if sys.platform == "win32":
    import os

    # Windows only adds a loading DLL's own directory to its dependency
    # search (see Python/dynload_win.c's LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR),
    # not that directory's parent -- unlike @loader_path/$ORIGIN on
    # macOS/Linux, there's no relative-path traversal at the PE/link level.
    # sqlite_rs.sqlite3._sqlite3 (one directory below this file) needs this
    # directory added explicitly to find the bundled sqlite_rs_libsqlite3.dll
    # (see shared_lib_name() in build.rs for why it isn't just sqlite3.dll);
    # _core (same directory) doesn't strictly need it but this is harmless
    # defense-in-depth for it too. Must run before any native import below.
    _ = os.add_dll_directory(str(Path(__file__).parent))

from sqlite_rs._core import (
    DEBUG_BUILD,  # pyright: ignore[reportUnknownVariableType]
    execute_and_fetch_all,  # pyright: ignore[reportUnknownVariableType]
    execute_and_fetch_all_via_raw_pointer,  # pyright: ignore[reportUnknownVariableType]
    execute_and_fetch_table,  # pyright: ignore[reportUnknownVariableType]
    execute_and_fetch_table_via_raw_pointer,  # pyright: ignore[reportUnknownVariableType]
    execute_many,  # pyright: ignore[reportUnknownVariableType]
    execute_many_via_raw_pointer,  # pyright: ignore[reportUnknownVariableType]
    fetch_all,  # pyright: ignore[reportUnknownVariableType]
    fetch_all_via_raw_pointer,  # pyright: ignore[reportUnknownVariableType]
    fetch_table,  # pyright: ignore[reportUnknownVariableType]
    fetch_table_via_raw_pointer,  # pyright: ignore[reportUnknownVariableType]
    get_raw_db_ptr,  # pyright: ignore[reportUnknownVariableType]
    get_raw_stmt_ptr,  # pyright: ignore[reportUnknownVariableType]
)

_LIBSQLITE3_NAMES = {
    "darwin": "libsqlite_rs_sqlite3.dylib",
    "win32": "sqlite_rs_libsqlite3.dll",
}

#: True when the extension was built without optimization, which costs roughly
#: 3x on a large fetch. The benchmark script refuses to report numbers from
#: such a build.
DEBUG_BUILD: bool

#: Path to the libsqlite3 dylib/so bundled alongside this package's native
#: modules -- the one library ``sqlite3.connect``, :func:`execute_and_fetch_all`,
#: and :func:`get_raw_db_ptr` all dynamically link against. Mirrors the
#: platform naming build.rs's shared_lib_name() uses to build it. Meant for
#: handing to an unrelated FFI caller, e.g.
#: ``ctypes.CDLL(sqlite_rs.LIBSQLITE3_PATH)``.
_LIBSQLITE3_NAME = _LIBSQLITE3_NAMES.get(sys.platform, "libsqlite_rs_sqlite3.so")
LIBSQLITE3_PATH = Path(__file__).parent / _LIBSQLITE3_NAME

__all__ = [
    "DEBUG_BUILD",
    "LIBSQLITE3_PATH",
    "execute_and_fetch_all",
    "execute_and_fetch_all_via_raw_pointer",
    "execute_and_fetch_table",
    "execute_and_fetch_table_via_raw_pointer",
    "execute_many",
    "execute_many_via_raw_pointer",
    "fetch_all",
    "fetch_all_via_raw_pointer",
    "fetch_table",
    "fetch_table_via_raw_pointer",
    "get_raw_db_ptr",
    "get_raw_stmt_ptr",
]
