#!/usr/bin/env python3
"""Compare sqlite_rs.execute_many against ADBC's ingest and stdlib executemany.

All three write the same rows into an equivalent table in a fresh database,
under WAL with synchronous=NORMAL, so what is measured is the three paths
rather than three journal modes.
sqlite_rs and ADBC are given the Arrow table directly; stdlib is given a list
of Python tuples, built before the clock starts, because that is the shape it
takes and converting for it is not what is being measured. That makes this, as
in the fetch benchmark, the reading most favourable to stdlib.

Usage:
    python scripts/benchmark_insert.py                 # 2M rows, every mode
    python scripts/benchmark_insert.py --rows 500_000
    python scripts/benchmark_insert.py --mode sqlite_rs

Needs the `bench` and `arrow` dependency groups, and a release build of the
extension -- a debug one is several times slower, and is refused rather than
reported:

    uv sync --group bench --group arrow
    uv run maturin develop --uv --release
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import importlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from benchmark_fetch_all import Result, peak_rss_bytes, require_release_build

if TYPE_CHECKING:
    from collections.abc import Callable

    import pyarrow as pa

SCHEMA = "CREATE TABLE t (i INTEGER, r REAL, s TEXT, b BLOB)"
# Every mode writes under the same settings, or this measures journal modes
# rather than the three paths. WAL keeps writers out of the rollback journal;
# NORMAL stops fsyncing every commit, which is what WAL makes safe enough to
# skip. The journal mode belongs to the file and is set once, before anyone
# opens it; synchronous belongs to a connection, so each mode sets its own.
JOURNAL = "PRAGMA journal_mode = WAL"
SYNCHRONOUS = "PRAGMA synchronous = NORMAL"
INSERT = "INSERT INTO t VALUES (?, ?, ?, ?)"
WARMUP_ROWS = 20_000


def source_table(rows: int) -> Any:  # noqa: ANN401
    """Return the rows every mode inserts, as one Arrow table.

    The same shape the fetch benchmark reads: one column per storage class,
    with text and blobs on both sides of the length where Arrow stops packing
    a value into its view.
    """
    import pyarrow as pa  # noqa: PLC0415

    numbers = range(rows)
    return pa.table(
        {
            "i": pa.array(numbers, pa.int64()),
            "r": pa.array([n * 1.5 for n in numbers], pa.float64()),
            "s": pa.array([f"row-{n}-{'x' * (n % 24)}" for n in numbers], pa.string()),
            "b": pa.array([bytes(4 + (n % 24)) for n in numbers], pa.binary()),
        }
    )


def prepared(db: Path) -> Path:
    """Put `db` in WAL before any mode opens it."""
    import sqlite3  # noqa: PLC0415

    with contextlib.closing(sqlite3.connect(db)) as conn:
        # Fetched, not just executed: a journal_mode pragma returns a row, and
        # the mode does not change until the statement is stepped to
        # completion. Leaving it unfetched leaves the database in its old mode
        # and still locked -- which silently benchmarked delete mode for WAL.
        _ = conn.execute(JOURNAL).fetchall()
    return db


def insert_via_sqlite_rs(db: Path, data: Any) -> int:  # noqa: ANN401
    import sqlite_rs  # noqa: PLC0415
    import sqlite_rs.sqlite3  # noqa: PLC0415

    # closing(), not the connection itself: a sqlite3 connection used as a
    # context manager is a transaction, not a handle to close. Closed on the
    # clock, as ADBC's context manager already is, and because Windows will
    # not let the scratch directory go while the file is still open.
    with contextlib.closing(sqlite_rs.sqlite3.connect(str(db))) as conn:
        _ = conn.execute(SYNCHRONOUS)
        _ = conn.execute(SCHEMA)
        inserted = sqlite_rs.execute_many(conn, INSERT, data)
        conn.commit()
        return inserted


def insert_via_adbc(db: Path, data: Any) -> int:  # noqa: ANN401
    from adbc_driver_sqlite import dbapi as adbc  # noqa: PLC0415

    with adbc.connect(str(db)) as conn, conn.cursor() as cursor:
        # SQLite will not change synchronous inside a transaction and ADBC
        # opens one implicitly, so autocommit goes on for just the pragma.
        conn.adbc_connection.set_autocommit(True)
        _ = cursor.execute(SYNCHRONOUS)
        conn.adbc_connection.set_autocommit(False)
        # The driver's own bulk path: it takes Arrow directly, as we do.
        inserted = cursor.adbc_ingest("t", data, mode="create")
        # Committed explicitly. Autocommit is off for the ingest, and closing
        # the connection discards an open transaction rather than committing
        # it, so without this the rows never land and the benchmark times work
        # that is thrown away -- which it did, flattering ADBC throughout.
        conn.commit()
        return inserted


def insert_via_stdlib(db: Path, data: Any) -> int:  # noqa: ANN401
    import sqlite3  # noqa: PLC0415

    rows = cast("list[tuple[object, ...]]", data)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        _ = conn.execute(SYNCHRONOUS)
        _ = conn.execute(SCHEMA)
        _ = conn.executemany(INSERT, rows)
        conn.commit()
        return len(rows)


INSERTERS: dict[str, Callable[[Path, Any], int]] = {
    "sqlite_rs": insert_via_sqlite_rs,
    "adbc": insert_via_adbc,
    "stdlib": insert_via_stdlib,
}
IMPORTS = {
    "sqlite_rs": ("sqlite_rs", "sqlite_rs.sqlite3"),
    "adbc": ("adbc_driver_sqlite", "adbc_driver_sqlite.dbapi"),
    "stdlib": ("sqlite3",),
}


def verify(db: Path, mode: str, claimed: int) -> None:
    """Fail unless the rows a mode claims to have inserted actually landed.

    A mode that leaves its transaction open is timed for work the database
    then throws away. ADBC did exactly that -- it reported two million rows
    and left no table behind -- so the count is checked from a fresh
    connection rather than taken on trust.
    """
    import sqlite3  # noqa: PLC0415

    with contextlib.closing(sqlite3.connect(db)) as conn:
        landed = cast("int", conn.execute("SELECT count(*) FROM t").fetchone()[0])
    if landed != claimed:
        msg = f"{mode} claimed {claimed:,} rows but {landed:,} are in the database"
        raise RuntimeError(msg)


def run_child(mode: str, rows: int) -> None:
    """Time one mode in this process and report it as JSON on stdout."""
    for module in IMPORTS[mode]:
        _ = importlib.import_module(module)

    table = source_table(rows)
    # stdlib takes tuples, built here so the conversion is not on the clock.
    data = cast("pa.Table", table).to_pylist() if mode == "stdlib" else table
    if mode == "stdlib":
        data = [tuple(row.values()) for row in cast("list[dict[str, object]]", data)]

    insert = INSERTERS[mode]
    with tempfile.TemporaryDirectory() as scratch:
        # The same path on a slice of the same rows, before anything is timed.
        warm = cast("pa.Table", table).slice(0, WARMUP_ROWS)
        starter = data[:WARMUP_ROWS] if mode == "stdlib" else warm
        _ = insert(prepared(Path(scratch) / "warm.db"), starter)

        bench = prepared(Path(scratch) / "bench.db")
        started = time.perf_counter()
        inserted = insert(bench, data)
        seconds = time.perf_counter() - started
        verify(bench, mode, inserted)

    result = Result(
        mode=mode, seconds=seconds, rows=inserted, peak_rss=peak_rss_bytes()
    )
    print(json.dumps(dataclasses.asdict(result)))


def run_mode(mode: str, rows: int) -> Result:
    print(f"  {mode} ...", end="", flush=True)
    finished = subprocess.run(  # noqa: S603
        [sys.executable, __file__, "--child", mode, "--rows", str(rows)],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        detail = finished.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"exited {finished.returncode}"
        print(f"\r  {mode}: FAILED -- {reason}")
        return Result(mode=mode, error=reason)

    result = Result.from_json(finished.stdout)
    print(f"\r  {mode}: {result.seconds:.2f}s, peak RSS {result.peak_rss / 1e9:.2f} GB")
    return result


def report(results: list[Result], rows: int) -> None:
    print(f"\n  {'mode':<12} {'seconds':>9} {'rows/s':>14} {'peak RSS':>11}")
    print(f"  {'-' * 12} {'-' * 9} {'-' * 14} {'-' * 11}")
    for result in results:
        if result.error:
            print(f"  {result.mode:<12} {result.error:>9}")
            continue
        rate = rows / result.seconds
        row = f"{result.seconds:>9.2f} {rate:>14,.0f} {result.peak_rss / 1e9:>8.2f} GB"
        print(f"  {result.mode:<12} {row}")

    done = {r.mode: r for r in results if not r.error}
    ours = done.get("sqlite_rs")
    if ours is None:
        return
    for mode, other in done.items():
        if mode != "sqlite_rs":
            print(f"\n  vs {mode}: {other.seconds / ours.seconds:.1f}x faster")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--rows", type=int, default=2_000_000)
    _ = parser.add_argument("--mode", choices=["all", *INSERTERS], default="all")
    _ = parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    child = cast("str | None", args.child)
    rows = cast("int", args.rows)
    mode = cast("str", args.mode)

    if child:
        run_child(child, rows)
        return

    print(f"\nsqlite_rs insert benchmark -- {rows:,} rows, four columns\n")
    modes = list(INSERTERS) if mode == "all" else [mode]
    if "sqlite_rs" in modes:
        require_release_build()
    report([run_mode(m, rows) for m in modes], rows)
    print()


if __name__ == "__main__":
    main()
