import ctypes
from pathlib import Path

from sqlite_rs._sqlite3 import Connection as Connection
from sqlite_rs._sqlite3 import connect as connect

LIBSQLITE3_PATH: Path

def query_via_rust(connection: Connection, sql: str) -> list[list[object]]: ...
def get_raw_db_ptr(connection: Connection) -> ctypes.c_void_p: ...
def query_via_raw_pointer(db_ptr: int, sql: str) -> list[list[object]]: ...
