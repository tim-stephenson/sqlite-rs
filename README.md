# sqlite-rs

A drop-in superset of Python's `sqlite3` bindings, extended with Rust.

`sqlite_rs.sqlite3` is a from-scratch build of CPython's own `sqlite3` module,
used exactly like it. On top of that, `sqlite_rs` adds Rust functions that act
on the *same* live connection: the clone module, the Rust extension and raw
`ctypes` all dynamically link one bundled SQLite, so a `sqlite3*` or
`sqlite3_stmt*` can be handed between them rather than reopened.

```python
import sqlite_rs
import sqlite_rs.sqlite3

conn = sqlite_rs.sqlite3.connect(":memory:")  # exactly like stdlib sqlite3
conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")

sqlite_rs.execute_and_fetch_all(conn, "INSERT INTO t VALUES (1, 'x')")

# One Arrow array per column, each as long as the number of rows.
a, b = sqlite_rs.execute_and_fetch_all(conn, "SELECT * FROM t")
len(a), len(b)  # (1, 1)
a.__arrow_c_array__()  # -> pyarrow.array(a), polars.from_arrow(a), ...

cur = conn.execute("SELECT * FROM t")
sqlite_rs.get_raw_stmt_ptr(cur)  # ctypes.c_void_p -> sqlite3_stmt*
sqlite_rs.fetch_all(cur)  # same columns -- and leaves cur exhausted

sqlite_rs.get_raw_db_ptr(conn)  # ctypes.c_void_p -> sqlite3*
sqlite_rs.LIBSQLITE3_PATH  # the bundled library, for ctypes.CDLL
```

Results are columnar. Each array exports `__arrow_c_array__` and
`__arrow_c_schema__`, so pyarrow, polars and duckdb consume them without a
copy -- and without sqlite_rs depending on any Arrow package itself.

SQLite types values per row, not per column, so a column is accumulated into
one contiguous typed buffer on the assumption the type it has been seeing is
the type that will keep coming. A wider value promotes what has accumulated,
once, along SQLite's own storage class order:

```text
NULL  <  INTEGER  <  REAL     <  TEXT       <  BLOB
null     int64      float64      utf8_view     binary_view
```

TEXT and BLOB become Arrow's view layouts rather than the older
offset-and-data ones, because views are what polars, DataFusion and arrow-rs
hold strings in -- handing over anything else costs the consumer a conversion
of the whole column.

NULL never promotes anything, it only contributes a null slot; a column of
nothing but NULLs stays Arrow `null`. Promotion is one-way, so a column is
rebuilt at most four times however many rows it has.

Supported on CPython 3.11-3.15, including free-threaded 3.15t, for Linux
(glibc and musl), macOS and Windows.


## API

Everything here acts on a live connection or statement from
`sqlite_rs.sqlite3`, never the stdlib `sqlite3`: the extension finds the
`sqlite3*` and `sqlite3_stmt*` inside those objects by reading CPython's
private struct layout, which is only guaranteed to match for objects this
package's own clone module built. A stdlib object raises `TypeError`.

| | |
| --- | --- |
| `execute_and_fetch_all(connection, sql)` | run one statement, return its columns |
| `fetch_all(cursor)` | drain a cursor's statement, return its columns |
| `execute_and_fetch_table(connection, sql)` | the same, as one table |
| `fetch_table(cursor)` | the same, as one table |
| `get_raw_db_ptr(connection)` | the `sqlite3*`, as a `ctypes.c_void_p` |
| `get_raw_stmt_ptr(cursor)` | the `sqlite3_stmt*`, as a `ctypes.c_void_p` |
| `*_via_raw_pointer(...)` | all four again, from a pointer |
| `LIBSQLITE3_PATH` | the bundled library, for `ctypes.CDLL` |
| `DEBUG_BUILD` | whether the extension was built without optimization |

The `_all` pair returns one array per result column, and `[]` for a statement
with no result columns, such as an `INSERT`; the `_table` pair returns the
same columns as one table, empty for such a statement. `sql` must hold a
single statement: a second one raises `ValueError` rather than being silently
dropped, and a SQL error is a `ValueError` carrying SQLite's own message.

`fetch_all` steps the very statement its cursor iterates, so the two share
position -- rows it returns are rows the cursor will no longer yield. It
leaves the cursor exhausted but usable; `execute()` it again to reuse it.

The `_via_raw_pointer` functions are the mirror image: they take a pointer an
unrelated FFI caller already holds, so the sharing works in both
directions. There is no object to check, so the pointer is trusted as given.

`help()` on any of these has the rest.

### Handing the columns on

`fetch_all` gives you the columns separately, each carrying its name in the
schema it exports, so a consumer that reads the schema needs nothing else:

```python
import polars as pl

columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT a, b AS renamed FROM t")
pl.DataFrame([pl.Series(c) for c in columns]).columns  # ['a', 'renamed']
```

It has to be column by column: a plain list is *data* to `pl.DataFrame`, not a
container of columns, so `pl.DataFrame(columns)` gives one object-typed column
holding the arrays themselves. `fetch_table` exists for that -- the same
arrays behind one `__arrow_c_stream__`, which a whole-table consumer takes in
a single call:

```python
pl.DataFrame(sqlite_rs.execute_and_fetch_table(conn, sql)).columns
```

Neither form copies. At 10M rows across all four storage classes, with text
and blobs on both sides of the length where Arrow stops packing a value into
its view, building the frame off either one costs no measurable time and no
measurable memory: polars adopts the buffers as they are.


## Development

Needs Rust 1.98 or newer (see `rust-version` in Cargo.toml for why) and
[uv](https://docs.astral.sh/uv/).

| | |
| --- | --- |
| build | `uv run maturin develop --uv` |
| test | `uv run pytest tests/` |
| lint | `uv run ruff check . && uv run ruff format --check .` |
| types | `uv run basedpyright . --warnings` |

`maturin develop` is the whole build: `build.rs` compiles the vendored SQLite
and CPython `_sqlite` sources, materializes `python/sqlite_rs/sqlite3/` from
`vendor/`, then builds `_core`. Every `uv run` re-syncs the project first, so
add `--no-sync` to skip the rebuild once it is current.

`uv sync --group bench` then `python scripts/benchmark_fetch_all.py` compares
fetching a large table into polars against ADBC's SQLite driver and stdlib
`sqlite3`. It needs `maturin develop --release`; `sqlite_rs.DEBUG_BUILD` says
which you have. On one machine, 10M rows of a four-column STRICT table:

| | seconds | peak RSS |
| --- | --- | --- |
| sqlite_rs | 0.94 | 0.48 GB |
| adbc | 2.23 | 1.58 GB |
| stdlib `fetchall()` | 4.28 | 2.56 GB |

The `Benchmark` workflow runs the same thing on CI, one interpreter, by hand.
10M rows again, seconds and peak RSS:

| | sqlite_rs | adbc | stdlib |
| --- | --- | --- | --- |
| windows-latest, x64 | 1.60 / 0.80 GB | 2.13 / 1.49 GB | 8.27 / 2.81 GB |
| ubuntu-latest, x64 | 1.85 / 0.81 GB | 4.09 / 1.74 GB | 7.86 / 2.76 GB |
| macos-latest, arm64 | 1.94 / 0.79 GB | 4.65 / 1.27 GB | 9.45 / 2.74 GB |

Read those down a column, not across: a runner's absolute speed moves by up to
2x between runs, so the same code has measured 1.57s and 2.87s on Windows.
Only the three modes within one run are comparable, having shared a machine.

Every mode starts warm -- the file is read once before any of them run, their
modules are imported before their clock starts, and each fetches 50k rows
through its own path first -- so the first two repeat to within a percent or
two. stdlib swings around 10% whatever you do, which is its allocator.

ADBC is the near neighbour -- Arrow out of its own bundled SQLite -- and is
given a batch size worth having rather than its 1024-row default. stdlib is
let off lightest of the three: `fetchall()` is where it stops, so it is not
charged for arranging its rows into anything.

`bear -- cargo build` regenerates `compile_commands.json`, which clangd needs to
resolve the vendored CPython headers in `native/`.

The bundled SQLite is deliberately named `libsqlite_rs_sqlite3`, not
`libsqlite3`: musl and Windows resolve a dependency by matching an
already-loaded module's bare filename, so an unprefixed name silently binds to
CPython's own SQLite instead. `.cargo/config.toml` exists to force that name
past libsqlite3-sys, which hardcodes `sqlite3`.


## TODO


### high

- Add CI for building docs

### nice to have

- Use `pyo3-stub-gen`
- Expand pytest tests to parametrize over the three ways to interact with `sqlite` (`ctypes` on the C library, `sqlite_rs.sqlite3`, or rust functions in `sqlite_rs._core`)
- Clean up the scripts which vendor `sqlite`, the cpython `sqlite` wrapper, the `basedpyright` cpython `sqlite` type stubs
- Look for improvements in the reliability of the method used to extract the sqlite connection from the python sqlite connection in `native/`
- Add test jobs for the targets that build but are untested: linux i686, win32, win_arm64
- Gate the release job on the test jobs; it currently only needs the build jobs
