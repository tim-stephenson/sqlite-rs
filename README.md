# sqlite-rs

A drop-in superset of Python's `sqlite3` bindings, extended with Rust.

`sqlite_rs.sqlite3` is a from-scratch build of CPython's own `sqlite3` module,
used exactly like it. On top of that, `sqlite_rs` adds Rust functions that read
the *same* live connection and hand back Arrow columns instead of rows of
Python objects.

```python
import polars as pl
import sqlite_rs
import sqlite_rs.sqlite3

conn = sqlite_rs.sqlite3.connect("example.db")  # exactly like stdlib sqlite3
conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
conn.execute("INSERT INTO t VALUES (1, 'x')")

# One Arrow column per result column, named, and copied into polars nowhere.
columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT * FROM t")
pl.DataFrame([pl.Series(c) for c in columns])
```

Ten million rows of a four-column table, into a polars `DataFrame`: **0.94s and
0.48 GB**, against stdlib `fetchall()`'s 4.28s and 2.56 GB — and `fetchall()`
is not even charged for building the frame.

Supported on CPython 3.11-3.15, including free-threaded 3.15t, for Linux
(glibc and musl), macOS and Windows.

Documentation: quick start, the API, and the benchmarks in full are in `docs/`
(`uv run sphinx-autobuild docs/source docs/build`).


## Development

Needs Rust 1.98 or newer (see `rust-version` in Cargo.toml for why) and
[uv](https://docs.astral.sh/uv/).

| | |
| --- | --- |
| build | `uv run maturin develop --uv` |
| test | `uv run pytest tests/` |
| lint | `uv run ruff check . && uv run ruff format --check .` |
| types | `uv run basedpyright . --warnings` |
| docs | `uv run sphinx-autobuild docs/source docs/build` |
| bench | `uv run python scripts/benchmark_fetch_all.py` |

`maturin develop` is the whole build: `build.rs` compiles the vendored SQLite
and CPython `_sqlite` sources, materializes `python/sqlite_rs/sqlite3/` from
`vendor/`, then builds `_core`. Every `uv run` re-syncs the project first, so
add `--no-sync` to skip the rebuild once it is current.

The benchmark needs `--release`; `sqlite_rs.DEBUG_BUILD` says which build you
have, and the script refuses to report numbers from a debug one.

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
