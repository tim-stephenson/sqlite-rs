# pyright: reportAny=false, reportExplicitAny=false
"""The long-running native calls must let other Python threads run.

Binding rows and decoding them touches only SQLite and Arrow, so holding the
interpreter lock through either would stall every other thread in the process
for the whole call.

Measured as a rate rather than a count. A thread that does nothing but
increment is timed twice: once while the main thread sleeps, which is the
free-running speed, and once while the native call is in flight. Counting
alone proves nothing, because the interpreter hands the lock over at the
bytecode boundaries either side of the call and the spinner banks a full
switch interval there whether or not the call itself ever yields.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import pytest
import sqlite_rs
import sqlite_rs.sqlite3

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pa = pytest.importorskip("pyarrow")

# Long enough that one switch interval of leakage at the edges is noise.
ROWS = 2_000_000
BASELINE_SECONDS = 0.3
# A call that yields runs the spinner at close to full speed; one that does
# not leaves it at a percent or two. Anywhere near half is unambiguous.
SHARE = 0.4


class Spinner:
    """A thread that counts as fast as the interpreter will let it."""

    def __init__(self) -> None:
        self.ticks = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop:
            self.ticks += 1

    def rate_while(self, work: Callable[[], object]) -> float:
        """Ticks per second achieved while `work` runs."""
        before, started = self.ticks, time.perf_counter()
        work()
        elapsed = time.perf_counter() - started
        return (self.ticks - before) / elapsed

    def __enter__(self) -> Self:
        self._thread.start()
        time.sleep(0.05)  # let it reach the loop
        return self

    def __exit__(self, *_: object) -> None:
        self._stop = True
        self._thread.join(timeout=5)


@pytest.fixture
def loaded(tmp_path: Path) -> Iterator[tuple[Any, Any]]:
    db = tmp_path / "gil.db"
    with contextlib.closing(sqlite3.connect(db)) as setup:
        _ = setup.execute("PRAGMA journal_mode = WAL").fetchall()
    conn = sqlite_rs.sqlite3.connect(str(db))
    _ = conn.execute("PRAGMA synchronous = NORMAL")
    _ = conn.execute("CREATE TABLE t (i INTEGER, s TEXT)")
    table = pa.table(
        {
            "i": pa.array(range(ROWS), pa.int64()),
            "s": pa.array([f"row-{n}" for n in range(ROWS)], pa.string()),
        }
    )
    try:
        yield conn, table
    finally:
        conn.close()


def test_execute_many_lets_other_threads_run(loaded: tuple[Any, Any]) -> None:
    conn, table = loaded
    with Spinner() as spinner:
        free = spinner.rate_while(lambda: time.sleep(BASELINE_SECONDS))
        during = spinner.rate_while(
            lambda: sqlite_rs.execute_many(conn, "INSERT INTO t VALUES (?, ?)", table)
        )
    held = f"spinner ran at {during / free:.1%} of free speed: the insert held the GIL"
    assert during > free * SHARE, held


def test_fetch_lets_other_threads_run(loaded: tuple[Any, Any]) -> None:
    conn, table = loaded
    _ = sqlite_rs.execute_many(conn, "INSERT INTO t VALUES (?, ?)", table)
    conn.commit()
    out: Any = None

    def fetch() -> None:
        nonlocal out
        out = sqlite_rs.execute_and_fetch_table(conn, "SELECT * FROM t")

    with Spinner() as spinner:
        free = spinner.rate_while(lambda: time.sleep(BASELINE_SECONDS))
        during = spinner.rate_while(fetch)
    assert out.num_rows == ROWS
    held = f"spinner ran at {during / free:.1%} of free speed: the query held the GIL"
    assert during > free * SHARE, held
