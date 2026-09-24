# pyright: reportAny=false, reportExplicitAny=false
# polars is not a declared dependency -- see the module docstring -- so
# everything reached through it is untyped here.
"""Inserting from polars, whose Arrow export is not quite the same as pyarrow's.

A null column has no physical storage, so the C data interface gives it no
buffers: `n_buffers` must be 0. polars exports 1, leaving the pointer null.
pyarrow imports that happily; arrow-rs believes the count, asks how wide
buffer 0 of a null array is, and fails. An all-null column is an ordinary
thing to have -- `pl.DataFrame({"a": [None, None]})` is Null dtype -- and the
error named none of that, so the count is repaired on import.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlite_rs
import sqlite_rs.sqlite3

pl = pytest.importorskip("polars")


_DDL = "CREATE TABLE t (a, b)"
_SQL = "INSERT INTO t VALUES (?, ?)"


def _insert(data: Any, ddl: str = _DDL, sql: str = _SQL) -> list[Any]:  # noqa: ANN401
    """Insert one frame into an untyped table and read every row back."""
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute(ddl)
    inserted = sqlite_rs.execute_many(conn, sql, data)
    rows = conn.execute("SELECT * FROM t").fetchall()
    assert inserted == len(rows)
    return rows


def test_a_null_dtype_column_inserts_as_null() -> None:
    frame = pl.DataFrame({"a": [None, None], "b": [None, None]})
    assert frame.schema["a"] == pl.Null  # the dtype this is really about
    assert _insert(frame) == [(None, None), (None, None)]


def test_a_null_dtype_column_beside_a_typed_one() -> None:
    frame = pl.DataFrame(
        {"a": [None, None], "b": ["x", "y"]}, schema={"a": pl.Null, "b": pl.Utf8}
    )
    assert _insert(frame) == [(None, "x"), (None, "y")]


def test_every_column_null_dtype() -> None:
    frame = pl.DataFrame({"a": [None], "b": [None], "c": [None]})
    rows = _insert(frame, "CREATE TABLE t (a, b, c)", "INSERT INTO t VALUES (?, ?, ?)")
    assert rows == [(None, None, None)]


@pytest.mark.parametrize(
    "dtype", [pl.Int64, pl.Float64, pl.Utf8, pl.Boolean, pl.Binary], ids=str
)
def test_a_typed_column_of_only_nulls(dtype: Any) -> None:  # noqa: ANN401
    """The neighbouring case, which never broke: typed, but every value null."""
    schema = {"a": dtype, "b": dtype}
    frame = pl.DataFrame({"a": [None, None], "b": [None, None]}, schema=schema)
    assert _insert(frame) == [(None, None), (None, None)]


def test_ordinary_polars_frames_still_insert() -> None:
    frame = pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    assert _insert(frame) == [(1, "x"), (2, "y"), (3, "z")]


def test_a_null_dtype_column_among_many_batches() -> None:
    """The repair is per batch, so it has to hold past the first one."""
    rows = 5_000
    frame = pl.DataFrame(
        {"a": [None] * rows, "b": list(range(rows))},
        schema={"a": pl.Null, "b": pl.Int64},
    )
    got = _insert(frame)
    assert len(got) == rows
    assert got[0] == (None, 0)
    assert got[-1] == (None, rows - 1)


def test_an_empty_frame_with_a_null_column_inserts_nothing() -> None:
    frame = pl.DataFrame({"a": [], "b": []}, schema={"a": pl.Null, "b": pl.Utf8})
    assert _insert(frame) == []
