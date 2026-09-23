#!/usr/bin/env python3
"""Report what each insert mode actually does, phase by phase.

Written to explain why the insert benchmark ranks the three modes so
differently across platforms: it prints the settings each mode ends up
running under, which is not necessarily the ones the fixture was given.
"""

from __future__ import annotations

import contextlib
import itertools
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).parent))
from benchmark_insert import (
    INSERT,
    SCHEMA,
    SYNCHRONOUS,
    prepared,
    source_table,
)

if TYPE_CHECKING:
    from collections.abc import Callable

ROWS = 2_000_000
REPEATS = 3
PRAGMAS = ("journal_mode", "synchronous", "page_size", "cache_size", "locking_mode")


def observed(execute: Callable[[str], Any]) -> str:
    """Return the settings actually in effect, as seen from this connection."""
    seen: list[str] = []
    for pragma in PRAGMAS:
        try:
            seen.append(f"{pragma}={execute(f'PRAGMA {pragma}').fetchall()[0][0]}")
        except Exception as exc:  # noqa: BLE001
            seen.append(f"{pragma}=<{type(exc).__name__}>")
    return "  ".join(seen)


def wal_mb(db: Path) -> float:
    """Size of the write-ahead log beside `db`, in MB, or 0 if there is none."""
    wal = Path(str(db) + "-wal")
    return wal.stat().st_size / 1e6 if wal.exists() else 0.0


def report(mode: str, marks: list[tuple[str, float]], seen: str, wal: float) -> None:
    print(f"\n===== {mode} =====")
    print(f"  in effect: {seen}")
    print(f"  wal before close: {wal:.1f} MB")
    total = marks[-1][1] - marks[0][1]
    for (name, start), (_, end) in itertools.pairwise(marks):
        share = (end - start) / total * 100
        print(f"    {name:<16} {end - start:6.2f}s  {share:5.1f}%")
    print(f"    {'TOTAL':<16} {total:6.2f}s")


def via_sqlite_rs(db: Path, data: Any) -> None:  # noqa: ANN401
    import sqlite_rs  # noqa: PLC0415
    import sqlite_rs.sqlite3  # noqa: PLC0415

    marks = [("connect", time.perf_counter())]
    conn = sqlite_rs.sqlite3.connect(str(db))
    marks.append(("setup", time.perf_counter()))
    _ = conn.execute(SYNCHRONOUS)
    _ = conn.execute(SCHEMA)
    marks.append(("execute_many", time.perf_counter()))
    _ = sqlite_rs.execute_many(conn, INSERT, data)
    marks.append(("commit", time.perf_counter()))
    conn.commit()
    marks.append(("close", time.perf_counter()))
    seen, wal = observed(conn.execute), wal_mb(db)
    conn.close()
    marks.append(("done", time.perf_counter()))
    report("sqlite_rs", marks, seen, wal)


def via_stdlib(db: Path, data: Any) -> None:  # noqa: ANN401
    rows = [tuple(r.values()) for r in data.to_pylist()]
    marks = [("connect", time.perf_counter())]
    conn = sqlite3.connect(db)
    marks.append(("setup", time.perf_counter()))
    _ = conn.execute(SYNCHRONOUS)
    _ = conn.execute(SCHEMA)
    marks.append(("executemany", time.perf_counter()))
    _ = conn.executemany(INSERT, rows)
    marks.append(("commit", time.perf_counter()))
    conn.commit()
    marks.append(("close", time.perf_counter()))
    seen, wal = observed(conn.execute), wal_mb(db)
    conn.close()
    marks.append(("done", time.perf_counter()))
    report("stdlib", marks, seen, wal)


def via_adbc(db: Path, data: Any) -> None:  # noqa: ANN401
    from adbc_driver_sqlite import dbapi as adbc  # noqa: PLC0415

    marks = [("connect", time.perf_counter())]
    with adbc.connect(str(db)) as conn, conn.cursor() as cursor:
        marks.append(("setup", time.perf_counter()))
        conn.adbc_connection.set_autocommit(True)
        _ = cursor.execute(SYNCHRONOUS)
        conn.adbc_connection.set_autocommit(False)
        marks.append(("adbc_ingest", time.perf_counter()))
        _ = cursor.adbc_ingest("t", data, mode="create")
        marks.append(("commit", time.perf_counter()))
        conn.commit()
        marks.append(("close", time.perf_counter()))

        def execute(sql: str) -> Any:  # noqa: ANN401
            _ = cursor.execute(sql)
            return cursor

        seen, wal = observed(execute), wal_mb(db)
    marks.append(("done", time.perf_counter()))
    report("adbc", marks, seen, wal)


MODES: dict[str, Callable[[Path, Any], None]] = {
    "sqlite_rs": via_sqlite_rs,
    "adbc": via_adbc,
    "stdlib": via_stdlib,
}


def build_options() -> str:
    """Return the bundled SQLite's compile options bearing on write speed."""
    import sqlite_rs.sqlite3  # noqa: PLC0415

    keys = ("THREADSAFE", "DEFAULT_WAL", "AUTOCHECKPOINT", "OMIT", "WIN32", "MEMSTATUS")
    with contextlib.closing(sqlite_rs.sqlite3.connect(":memory:")) as conn:
        rows = conn.execute("PRAGMA compile_options").fetchall()
    return " ".join(sorted(r[0] for r in rows if any(k in r[0] for k in keys)))


def main() -> None:
    print(f"platform {sys.platform}, {ROWS:,} rows, {REPEATS} repeats")
    print(f"bundled SQLite: {build_options()}")
    table = source_table(ROWS)
    for mode, run in MODES.items():
        for attempt in range(REPEATS):
            with tempfile.TemporaryDirectory() as scratch:
                db = prepared(Path(scratch) / f"{mode}.db")
                with contextlib.closing(sqlite3.connect(db)) as check:
                    given = check.execute("PRAGMA journal_mode").fetchone()[0]
                label = f"[{attempt + 1}/{REPEATS}] {mode}"
                print(f"\n  {label}: fixture journal_mode={given}")
                run(db, table)


if __name__ == "__main__":
    main()
