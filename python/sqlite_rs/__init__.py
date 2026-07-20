"""sqlite_rs: a Rust reimplementation of SQLite.

Validated against real SQLite in-process: three things in this package
dynamically link the exact same bundled ``libsqlite3`` (see
``vendor/sqlite/``), so a live connection can be shared between them
without serializing/reopening:

- ``sqlite_rs.sqlite3`` -- a from-scratch build of CPython's own stdlib
  ``sqlite3`` package (vendored unmodified, see ``vendor/cpython/``): the
  DB-API 2.0 wrapper (``sqlite3.dbapi2``) around a C extension
  (``sqlite3._sqlite3``). Use it exactly like the stdlib ``sqlite3`` module,
  e.g. ``sqlite_rs.sqlite3.connect(...)``.
- ``sqlite_rs._core`` -- this project's Rust extension. :func:`query_via_rust`
  and :func:`get_raw_db_ptr` require a ``sqlite_rs.sqlite3.Connection``
  (not one from the stdlib ``sqlite3`` module); ``_core`` enforces that
  itself.
- The bundled ``libsqlite3`` dynamic library itself, at :data:`LIBSQLITE3_PATH`.
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
    # directory added explicitly to find libsqlite3's renamed sqlite3.dll;
    # _core (same directory) doesn't strictly need it but this is harmless
    # defense-in-depth for it too. Must run before any native import below.
    os.add_dll_directory(str(Path(__file__).parent))

from sqlite_rs._core import (
    get_raw_db_ptr,  # pyright: ignore[reportUnknownVariableType]
    query_via_raw_pointer,  # pyright: ignore[reportUnknownVariableType]
    query_via_rust,  # pyright: ignore[reportUnknownVariableType]
)

_LIBSQLITE3_NAMES = {"darwin": "libsqlite3.dylib", "win32": "sqlite3.dll"}

#: Path to the libsqlite3 dylib/so bundled alongside this package's native
#: modules -- the one library ``sqlite3.connect``, :func:`query_via_rust`,
#: and :func:`get_raw_db_ptr` all dynamically link against. Mirrors the
#: platform naming build.rs's shared_lib_name() uses to build it. Meant for
#: handing to an unrelated FFI caller, e.g.
#: ``ctypes.CDLL(sqlite_rs.LIBSQLITE3_PATH)``.
_LIBSQLITE3_NAME = _LIBSQLITE3_NAMES.get(sys.platform, "libsqlite3.so")
LIBSQLITE3_PATH = Path(__file__).parent / _LIBSQLITE3_NAME

__all__ = [
    "LIBSQLITE3_PATH",
    "get_raw_db_ptr",
    "query_via_raw_pointer",
    "query_via_rust",
]
