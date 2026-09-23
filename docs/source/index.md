# sqlite-rs

A drop-in superset of Python's `sqlite3` bindings, extended with Rust.

`sqlite_rs.sqlite3` is a from-scratch build of CPython's own `sqlite3` module,
used exactly like it — same API, same behaviour, vendored unmodified. On top of
that, `sqlite_rs` adds Rust functions that read the *same* live connection and
hand back Arrow columns instead of rows of Python objects.

## Why

Reading a large table through the DB-API costs a tuple per row and a Python
object per cell, and a dataframe library then has to transpose all of it. That
is most of the time and most of the memory in a big read.

`sqlite_rs` reads the same connection column-first, straight into Arrow
buffers, and hands those over without copying. Ten million rows of a
four-column table, into a polars `DataFrame`:

| | seconds | peak RSS |
| --- | --- | --- |
| `sqlite_rs` | 0.94 | 0.48 GB |
| stdlib `fetchall()`, not even building the frame | 4.28 | 2.56 GB |

## How it fits together

Three things in the package link one and the same bundled SQLite, so a
connection opened by one can be read by another — no second connection, no
re-reading, no serialising between them:

- **`sqlite_rs.sqlite3`** — the clone of CPython's `sqlite3`. Open connections
  and run statements exactly as you do today.
- **`sqlite_rs`** — the Rust extension, reading those connections into Arrow
  and writing Arrow back into them.
- **`sqlite_rs.LIBSQLITE3_PATH`** — the library itself, for a `ctypes` caller
  that wants to drive the same connection directly.

Nothing here requires an Arrow package to be installed. The columns are
exported over the Arrow PyCapsule interface, which polars, pyarrow and duckdb
all read natively.

## Threading

The bundled SQLite is built multi-thread rather than serialized, which is worth
roughly 15% on queries and 12% on inserts. The cost is that
`sqlite_rs.sqlite3.threadsafety` reports `1` where the standard library reports
`3`: threads may share the module, but one connection must not be used from
more than one of them, not even with `check_same_thread=False`.

In exchange the Rust calls hand the interpreter lock back for everything that
only touches SQLite and Arrow — decoding rows, binding them, and the commit —
so a long query or insert does not stall the rest of the process while it runs.

Supported on CPython 3.11–3.15, including free-threaded 3.15t, for Linux
(glibc and musl), macOS 12 and later, and Windows.

```{toctree}
:maxdepth: 2
:caption: Contents:
quick_start/index.md
api/index.md
performance/index.md
examples/index.md
```
