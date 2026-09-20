import ctypes
from pathlib import Path
from typing import Protocol

import sqlite_rs.sqlite3

# The bundled SQLite that this package, sqlite_rs.sqlite3 and any ctypes caller
# all link, e.g. ctypes.CDLL(str(LIBSQLITE3_PATH)).
LIBSQLITE3_PATH: Path

# True when the extension was built without optimization, which costs roughly
# 3x on a large fetch. Only the benchmark script reads it.
DEBUG_BUILD: bool

# An Arrow array exported over the PyCapsule interface. Anything speaking that
# protocol -- pyarrow, polars, duckdb, arro3 -- consumes these without a copy,
# and sqlite_rs needs no Arrow package of its own installed to produce them.
# The capsules are opaque PyCapsule objects with no public type.
class ArrowScalar(Protocol):
    def as_py(self) -> object: ...

class ArrowArrayExportable(Protocol):
    def __arrow_c_array__(
        self, requested_schema: object | None = None
    ) -> tuple[object, object]: ...
    def __arrow_c_schema__(self) -> object: ...
    def __len__(self) -> int: ...
    def __getitem__(self, i: int) -> ArrowScalar: ...

# One array per result column, each as long as the number of rows. Docstrings
# live on the functions themselves (PYI021): help() reads them from _core.
def execute_and_fetch_all(
    connection: sqlite_rs.sqlite3.Connection, sql: str
) -> list[ArrowArrayExportable]: ...
def fetch_all(cursor: sqlite_rs.sqlite3.Cursor) -> list[ArrowArrayExportable]: ...
def get_raw_db_ptr(connection: sqlite_rs.sqlite3.Connection) -> ctypes.c_void_p: ...
def get_raw_stmt_ptr(cursor: sqlite_rs.sqlite3.Cursor) -> ctypes.c_void_p: ...
def execute_and_fetch_all_via_raw_pointer(
    db_ptr: int | ctypes.c_void_p, sql: str
) -> list[ArrowArrayExportable]: ...
def fetch_all_via_raw_pointer(
    stmt_ptr: int | ctypes.c_void_p,
) -> list[ArrowArrayExportable]: ...
