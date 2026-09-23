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

# The same columns behind one object. Whole-table consumers -- polars'
# DataFrame, pyarrow's table -- take this in a single call.
class ArrowStreamExportable(Protocol):
    def __arrow_c_stream__(self, requested_schema: object | None = None) -> object: ...
    def __arrow_c_schema__(self) -> object: ...
    # Past the export protocol: enough to inspect the result without an Arrow
    # package installed. Reaching a column as an object needs one.
    @property
    def num_rows(self) -> int: ...
    @property
    def num_columns(self) -> int: ...
    @property
    def shape(self) -> tuple[int, int]: ...
    @property
    def column_names(self) -> list[str]: ...

# A table on its way *in*, which demands less than the one handed back: a
# polars DataFrame exports the stream but has no num_rows or column_names, and
# is a perfectly good thing to insert.
class ArrowTableInput(Protocol):
    def __arrow_c_stream__(self, requested_schema: object | None = None) -> object: ...

# One array per result column, each as long as the number of rows. Docstrings
# live on the functions themselves (PYI021): help() reads them from _core.
def execute_and_fetch_all(
    connection: sqlite_rs.sqlite3.Connection, sql: str
) -> list[ArrowArrayExportable]: ...
def fetch_all(cursor: sqlite_rs.sqlite3.Cursor) -> list[ArrowArrayExportable]: ...
def execute_and_fetch_table(
    connection: sqlite_rs.sqlite3.Connection, sql: str
) -> ArrowStreamExportable: ...
def fetch_table(cursor: sqlite_rs.sqlite3.Cursor) -> ArrowStreamExportable: ...
def execute_many(
    connection: sqlite_rs.sqlite3.Connection,
    sql: str,
    data: ArrowTableInput,
) -> int: ...
def execute_many_via_raw_pointer(
    db_ptr: int | ctypes.c_void_p, sql: str, data: ArrowTableInput
) -> int: ...
def get_raw_db_ptr(connection: sqlite_rs.sqlite3.Connection) -> ctypes.c_void_p: ...
def get_raw_stmt_ptr(cursor: sqlite_rs.sqlite3.Cursor) -> ctypes.c_void_p: ...
def execute_and_fetch_all_via_raw_pointer(
    db_ptr: int | ctypes.c_void_p, sql: str
) -> list[ArrowArrayExportable]: ...
def fetch_all_via_raw_pointer(
    stmt_ptr: int | ctypes.c_void_p,
) -> list[ArrowArrayExportable]: ...
def execute_and_fetch_table_via_raw_pointer(
    db_ptr: int | ctypes.c_void_p, sql: str
) -> ArrowStreamExportable: ...
def fetch_table_via_raw_pointer(
    stmt_ptr: int | ctypes.c_void_p,
) -> ArrowStreamExportable: ...
