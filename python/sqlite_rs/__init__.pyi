import ctypes
from pathlib import Path

import sqlite_rs.sqlite3

LIBSQLITE3_PATH: Path

def query_via_rust(connection: sqlite_rs.sqlite3.Connection, sql: str) -> list[list[object]]: ...
def get_raw_db_ptr(connection: sqlite_rs.sqlite3.Connection) -> ctypes.c_void_p: ...
def query_via_raw_pointer(db_ptr: int, sql: str) -> list[list[object]]: ...
