#!/usr/bin/env python3
"""Compare sqlite_rs against stdlib sqlite3 and ADBC, reading a whole table.

sqlite_rs goes all the way to a polars DataFrame, handing polars one Arrow
array per column over the PyCapsule interface, so the data never becomes
Python objects. ADBC is the near neighbour -- its SQLite driver also returns
Arrow, through its own bundled SQLite and its own connection -- and is taken
to a DataFrame the same way. stdlib sqlite3 stops at fetchall(), which is
already a tuple per row and a Python object per cell; it is not charged for
arranging those into anything.

Usage:
    python scripts/benchmark_fetch_all.py                  # 100M rows, both modes
    python scripts/benchmark_fetch_all.py --rows 5_000_000
    python scripts/benchmark_fetch_all.py --db /tmp/bench.db --keep

The table is STRICT, so every column holds exactly one storage class and
sqlite_rs's type promotion never fires -- the case worth measuring.

Every mode starts warm: the database is read once before any of them run, the
modules a mode needs are imported before its clock starts, and it fetches
WARMUP_ROWS rows through its own path first. What is left on the clock is the
work, not the first-time cost of getting to it.

Each mode runs in its own subprocess, so peak memory is attributed to one mode
and an out-of-memory in one does not take the other down with it. At 100M rows
the stdlib path needs tens of gigabytes; being unable to finish is itself a
result, and is reported as one.

Needs the `bench` dependency group, and a release build of the extension --
a debug one is roughly 3x slower, and is refused rather than reported:

    uv sync --group bench
    uv run maturin develop --uv --release
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import importlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar, cast, final

BATCH = 1_000_000
SCHEMA = """
CREATE TABLE t (
    i INTEGER NOT NULL,
    r REAL    NOT NULL,
    s TEXT    NOT NULL,
    b BLOB    NOT NULL
) STRICT
"""
# One statement per batch, generated inside SQLite: pushing 100M rows through
# executemany() would measure Python's parameter binding, not the database.
#
# The text and blob lengths straddle 12 bytes on purpose: 4 to 27, a bit over a
# third of them 12 or under. Arrow's view layout, which is what sqlite_rs
# builds both as, packs anything that short into the view itself and sends the
# rest to a data buffer. They are two different paths, and a fixture of
# uniformly short values -- which this was -- only ever measures the first.
FILL = """
INSERT INTO t (i, r, s, b)
WITH RECURSIVE seq(n) AS (
    SELECT ? UNION ALL SELECT n + 1 FROM seq WHERE n < ?
)
SELECT
    n,
    n * 1.5,
    substr(hex(randomblob(16)), 1, 4 + (n % 24)),
    randomblob(4 + (n % 24))
FROM seq
"""
COLUMNS = ("i", "r", "s", "b")
SELECT = "SELECT i, r, s, b FROM t"
# Enough rows for a warmup to touch every branch of a mode's path and settle
# its allocator, and few enough to cost nothing next to the run being timed.
WARMUP_ROWS = 50_000


@dataclasses.dataclass(frozen=True)
class Result:
    """One mode's outcome, passed from the child process as JSON."""

    mode: str
    seconds: float = 0.0
    # Of `seconds`, the part spent getting the data out of SQLite, before any
    # of it is handed to polars. Which side of that line a platform loses time
    # on is the first question worth asking about a slow one.
    read_seconds: float = 0.0
    rows: int = 0
    peak_rss: int = 0
    error: str | None = None

    @classmethod
    def from_json(cls, blob: str) -> Result:
        """Rebuild a Result from the JSON a child process printed."""
        raw = cast("dict[str, object]", json.loads(blob))
        return cls(
            mode=str(raw["mode"]),
            seconds=float(cast("float", raw.get("seconds", 0.0))),
            read_seconds=float(cast("float", raw.get("read_seconds", 0.0))),
            rows=int(cast("int", raw.get("rows", 0))),
            peak_rss=int(cast("int", raw.get("peak_rss", 0))),
            error=cast("str | None", raw.get("error")),
        )


@final
class _WindowsMemoryCounters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS, as far as the field this needs."""

    _fields_: ClassVar = (
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


def peak_rss_bytes() -> int:
    if sys.platform == "win32":
        # No getrusage on Windows; the peak working set is the counterpart.
        # Both signatures have to be spelled out: GetCurrentProcess returns a
        # pseudo-handle of (HANDLE)-1, and ctypes' default int restype
        # truncates it to 32 bits, after which GetProcessMemoryInfo fails and
        # leaves the counters zeroed.
        current_process = ctypes.windll.kernel32.GetCurrentProcess
        current_process.restype = ctypes.c_void_p
        read_counters = ctypes.windll.psapi.GetProcessMemoryInfo
        read_counters.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_WindowsMemoryCounters),
            ctypes.c_uint32,
        )
        read_counters.restype = ctypes.c_int

        counters = _WindowsMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not read_counters(current_process(), ctypes.byref(counters), counters.cb):
            message = f"GetProcessMemoryInfo failed: {ctypes.GetLastError()}"
            raise OSError(message)
        return int(counters.PeakWorkingSetSize)

    import resource  # noqa: PLC0415

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    return peak if sys.platform == "darwin" else peak * 1024


def build(db: Path, rows: int) -> None:
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    # Durability is irrelevant for a throwaway fixture and dominates the build.
    # journal_mode is fetched because the pragma returns a row and does not
    # take effect until the statement is stepped to completion.
    _ = conn.execute("PRAGMA journal_mode = OFF").fetchall()
    _ = conn.execute("PRAGMA synchronous = OFF")
    _ = conn.execute(SCHEMA)

    started = time.perf_counter()
    for start in range(1, rows + 1, BATCH):
        end = min(start + BATCH - 1, rows)
        _ = conn.execute(FILL, (start, end))
        conn.commit()
        done = end / rows
        elapsed = time.perf_counter() - started
        progress = f"{done:5.1%}, {elapsed:6.1f}s elapsed"
        print(f"\r  building {end:,}/{rows:,} rows ({progress})", end="", flush=True)
    conn.close()
    size = db.stat().st_size
    print(f"\r  built {rows:,} rows, {size / 1e9:.2f} GB on disk{' ' * 20}")


def fetch_via_sqlite_rs(db: Path, sql: str) -> tuple[int, float]:
    # Imported here, not at module scope, so only the child process doing this
    # mode pays for polars at all. IMPORTS above has already loaded it by the
    # time this runs, so the cost does not land inside the timed section.
    import polars as pl  # noqa: PLC0415
    import sqlite_rs  # noqa: PLC0415
    import sqlite_rs.sqlite3  # noqa: PLC0415

    conn = sqlite_rs.sqlite3.connect(str(db))
    started = time.perf_counter()
    columns = sqlite_rs.execute_and_fetch_all(conn, sql)
    read = time.perf_counter() - started
    # Each array's name rides along in its exported Arrow schema, so polars
    # names the Series itself.
    frame = pl.DataFrame([pl.Series(c) for c in columns])
    return frame.height, read


def fetch_via_adbc(db: Path, sql: str) -> tuple[int, float]:
    import polars as pl  # noqa: PLC0415
    from adbc_driver_sqlite import StatementOptions  # noqa: PLC0415
    from adbc_driver_sqlite import dbapi as adbc  # noqa: PLC0415

    with adbc.connect(str(db)) as conn, conn.cursor() as cursor:
        # The driver's default is 1024 rows a batch, which leaves polars
        # stitching ten thousand chunks together afterwards. Raising it is
        # what a user reading this table would do, so the comparison does too.
        batch = {StatementOptions.BATCH_ROWS.value: "65536"}
        cursor.adbc_statement.set_options(**batch)
        started = time.perf_counter()
        _ = cursor.execute(sql)
        table = cursor.fetch_arrow_table()
        read = time.perf_counter() - started
        frame = pl.DataFrame(table)
    return frame.height, read


def fetch_via_stdlib(db: Path, sql: str) -> tuple[int, float]:
    started = time.perf_counter()
    cursor = sqlite3.connect(db).execute(sql)
    rows = cursor.fetchall()
    # All of it: fetchall() is where this mode stops.
    return len(rows), time.perf_counter() - started


FETCH = {
    "sqlite_rs": fetch_via_sqlite_rs,
    "adbc": fetch_via_adbc,
    "stdlib": fetch_via_stdlib,
}
# Imported before the clock starts. polars alone is a few hundred milliseconds
# and pyarrow more, which is a large share of what is being measured.
IMPORTS = {
    "sqlite_rs": ("polars", "sqlite_rs", "sqlite_rs.sqlite3"),
    "adbc": ("polars", "adbc_driver_sqlite", "adbc_driver_sqlite.dbapi"),
    "stdlib": (),
}


def run_child(mode: str, db: Path) -> None:
    """Time one mode and report it as JSON on stdout."""
    fetch = FETCH[mode]
    for module in IMPORTS[mode]:
        _ = importlib.import_module(module)
    # The same path, on a slice of the same table, before anything is timed:
    # whatever a library defers to its first call, whatever the allocator has
    # to ask the kernel for, and the page cache for the start of the file.
    _ = fetch(db, f"{SELECT} LIMIT {WARMUP_ROWS}")
    started = time.perf_counter()
    try:
        height, read = fetch(db, SELECT)
    except MemoryError:
        print(json.dumps(dataclasses.asdict(Result(mode=mode, error="MemoryError"))))
        return
    result = Result(
        mode=mode,
        seconds=time.perf_counter() - started,
        read_seconds=read,
        rows=height,
        peak_rss=peak_rss_bytes(),
    )
    print(json.dumps(dataclasses.asdict(result)))


def require_release_build() -> None:
    """Refuse to report numbers from an unoptimized build of the extension."""
    import sqlite_rs  # noqa: PLC0415

    if sqlite_rs.DEBUG_BUILD:
        cost = "sqlite_rs was built without optimization: roughly 3x slower here."
        fix = "uv run maturin develop --uv --release"
        sys.exit(f"{cost}\n  Rebuild with: {fix}")


def warm_page_cache(db: Path) -> None:
    """Read the database once, so no mode pays for a cold file.

    Modes run in sequence, so without this the first one reads from disk and
    the rest from the page cache -- worth seconds on a file this size, and
    worth all of the difference between two modes that are otherwise close.
    """
    with db.open("rb") as handle:
        while handle.read(1 << 24):
            pass


def run_mode(mode: str, db: Path) -> Result:
    print(f"  {mode} ...", end="", flush=True)
    finished = subprocess.run(  # noqa: S603
        [sys.executable, __file__, "--child", mode, "--db", str(db)],
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        # A kill by the OOM killer arrives here rather than as MemoryError.
        detail = finished.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"exited {finished.returncode}"
        print(f"\r  {mode}: FAILED -- {reason}")
        return Result(mode=mode, error=reason)

    result = Result.from_json(finished.stdout)
    if result.error:
        print(f"\r  {mode}: FAILED -- {result.error}")
    else:
        peak = result.peak_rss / 1e9
        print(f"\r  {mode}: {result.seconds:.2f}s, peak RSS {peak:.2f} GB")
    return result


def report(results: list[Result], rows: int) -> None:
    header = f"{'seconds':>9} {'read':>8} {'rows/s':>14} {'peak RSS':>11}"
    print(f"\n  {'mode':<12} {header}")
    print(f"  {'-' * 12} {'-' * 9} {'-' * 8} {'-' * 14} {'-' * 11}")
    for result in results:
        if result.error:
            print(f"  {result.mode:<12} {result.error:>9}")
        else:
            rate = rows / result.seconds
            peak = result.peak_rss / 1e9
            row = (
                f"{result.seconds:>9.2f} {result.read_seconds:>8.2f} "
                f"{rate:>14,.0f} {peak:>8.2f} GB"
            )
            print(f"  {result.mode:<12} {row}")

    done = {r.mode: r for r in results if not r.error}
    ours = done.get("sqlite_rs")
    if ours is None:
        return
    for mode, other in done.items():
        if mode == "sqlite_rs":
            continue
        comparison = f"{other.seconds / ours.seconds:.1f}x faster"
        # Timings are the result; if a platform's peak RSS did not come back,
        # say so rather than lose the run to a division.
        if ours.peak_rss and other.peak_rss:
            comparison += f", {other.peak_rss / ours.peak_rss:.1f}x less memory"
        print(f"\n  vs {mode}: {comparison}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--rows", type=int, default=100_000_000)
    _ = parser.add_argument("--db", type=Path, default=Path("bench.db"))
    _ = parser.add_argument("--mode", choices=["all", *FETCH], default="all")
    _ = parser.add_argument(
        "--rebuild", action="store_true", help="rebuild even if the db exists"
    )
    _ = parser.add_argument(
        "--keep", action="store_true", help="keep the database afterwards"
    )
    _ = parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args()
    # argparse hands back an untyped Namespace.
    child = cast("str | None", args.child)
    db = cast("Path", args.db)
    rows = cast("int", args.rows)
    mode = cast("str", args.mode)

    if child:
        run_child(child, db)
        return

    print(f"\nsqlite_rs fetch benchmark -- {rows:,} rows, STRICT table\n")
    if cast("bool", args.rebuild) or not db.exists():
        build(db, rows)
    else:
        print(f"  reusing {db} ({db.stat().st_size / 1e9:.2f} GB); --rebuild to redo")

    modes = list(FETCH) if mode == "all" else [mode]
    if "sqlite_rs" in modes:
        require_release_build()
    warm_page_cache(db)
    report([run_mode(m, db) for m in modes], rows)

    if not cast("bool", args.keep):
        db.unlink(missing_ok=True)
    print()


if __name__ == "__main__":
    main()
