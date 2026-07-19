"""sqlite_rs: a Rust reimplementation of SQLite.

Validated against real SQLite in-process: three things in this package
dynamically link the exact same bundled ``libsqlite3`` (see
``vendor/sqlite/``), so a live connection can be shared between them
without serializing/reopening:

- ``sqlite_rs._sqlite3`` -- a from-scratch build of CPython's own
  ``Modules/_sqlite`` sources (vendored unmodified, see ``vendor/cpython/``),
  exposed here as :data:`connect`/:data:`Connection`.
- ``sqlite_rs._core`` -- this project's Rust extension. :func:`query_via_rust`
  and :func:`get_raw_db_ptr` require a :data:`Connection` from this package
  (not the stdlib ``sqlite3`` module); ``_core`` enforces that itself.
- The bundled ``libsqlite3`` dynamic library itself, at :data:`LIBSQLITE3_PATH`.
"""

import sys
from pathlib import Path

from sqlite_rs import _sqlite3 as _sqlite3_clone
from sqlite_rs._core import get_raw_db_ptr, query_via_raw_pointer, query_via_rust

Connection = _sqlite3_clone.Connection
connect = _sqlite3_clone.connect

_LIBSQLITE3_NAMES = {"darwin": "libsqlite3.dylib", "win32": "sqlite3.dll"}

#: Path to the libsqlite3 dylib/so bundled alongside this package's native
#: modules -- the one library :data:`connect`, :func:`query_via_rust`, and
#: :func:`get_raw_db_ptr` all dynamically link against. Mirrors the platform
#: naming build.rs's shared_lib_name() uses to build it. Meant for handing
#: to an unrelated FFI caller, e.g. ``ctypes.CDLL(sqlite_rs.LIBSQLITE3_PATH)``.
_LIBSQLITE3_NAME = _LIBSQLITE3_NAMES.get(sys.platform, "libsqlite3.so")
LIBSQLITE3_PATH = Path(__file__).parent / _LIBSQLITE3_NAME

__all__ = [
    "LIBSQLITE3_PATH",
    "Connection",
    "connect",
    "get_raw_db_ptr",
    "query_via_raw_pointer",
    "query_via_rust",
]
