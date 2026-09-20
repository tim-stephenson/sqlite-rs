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
NULL  <  INTEGER  <  REAL  <  TEXT  <  BLOB
 null    int64       float64  utf8     binary
```

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
| `get_raw_db_ptr(connection)` | the `sqlite3*`, as a `ctypes.c_void_p` |
| `get_raw_stmt_ptr(cursor)` | the `sqlite3_stmt*`, as a `ctypes.c_void_p` |
| `execute_and_fetch_all_via_raw_pointer(db_ptr, sql)` | the same, from a pointer |
| `fetch_all_via_raw_pointer(stmt_ptr)` | the same, from a pointer |
| `LIBSQLITE3_PATH` | the bundled library, for `ctypes.CDLL` |
| `DEBUG_BUILD` | whether the extension was built without optimization |

Both fetch functions return one array per result column, and `[]` for a
statement with no result columns, such as an `INSERT`. `sql` must hold a
single statement: a second one raises `ValueError` rather than being silently
dropped, and a SQL error is a `ValueError` carrying SQLite's own message.

`fetch_all` steps the very statement its cursor iterates, so the two share
position -- rows it returns are rows the cursor will no longer yield. It
leaves the cursor exhausted but usable; `execute()` it again to reuse it.

The two `_via_raw_pointer` functions are the mirror image: they take a pointer
an unrelated FFI caller already holds, so the sharing works in both
directions. There is no object to check, so the pointer is trusted as given.

`help()` on any of these has the rest.

### Handing the columns on

Each array carries its column name in the schema it exports, so a consumer
that reads the schema needs nothing else:

```python
import polars as pl

columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT a, b AS renamed FROM t")
frame = pl.DataFrame([pl.Series(c) for c in columns])
frame.columns  # ['a', 'renamed']
```

Per column rather than all at once, because an Arrow *array* has no name --
only the field describing it does -- so `pl.DataFrame(columns)` would see one
unnamed thing rather than two named ones.


## Development

Needs a Rust toolchain and [uv](https://docs.astral.sh/uv/).

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
fetching a large table into polars against stdlib `sqlite3`'s `fetchall()`. It
needs `maturin develop --release`; `sqlite_rs.DEBUG_BUILD` says which you have.
`fetchall()` is where stdlib stops -- it is not charged for arranging its rows
into anything. On one machine, 10M rows of a four-column STRICT table: 0.97s
and 0.69 GB against 4.30s and 2.26 GB.

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
