# sqlite-rs

A drop-in superset of Python's `sqlite3` bindings, extended with Rust for
performant Arrow interoperability.

Ten million rows of a four-column table, read into a polars `DataFrame` --
seconds and peak memory, each row measured on one GitHub runner:

| | sqlite_rs | adbc | stdlib `sqlite3` |
| --- | --- | --- | --- |
| macOS, arm64 | **1.94s** / 0.79 GB | 4.65s / 1.27 GB | 9.45s / 2.74 GB |
| Linux, x86-64 | **1.85s** / 0.81 GB | 4.09s / 1.74 GB | 7.86s / 2.76 GB |
| Windows, x86-64 | **1.60s** / 0.80 GB | 2.13s / 1.49 GB | 8.27s / 2.81 GB |

stdlib is let off lightest of the three: `fetchall()` is where it stops, so it
is never charged for building the frame at all. Compare down a column, not
across -- a runner's own speed moves by up to 2x between runs, so only the
three that shared a machine are comparable.

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

Supported on CPython 3.11-3.15, including free-threaded 3.15t, for Linux
(glibc and musl), macOS and Windows.

Documentation, with a quick start, the API and the benchmarks in full:
<https://tim-stephenson.github.io/sqlite-rs/>. To work on it locally,
`uv run sphinx-autobuild docs/source docs/build`.


## AI policy

All of the code in this repository was written by an LLM. A human directed it
throughout: the higher-level structure and the design choices are theirs, and
the LLM wrote the code that followed from them. This documentation was written
the same way.


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

### worth doing

- Harden how `native/` finds the `sqlite3*` inside a Python connection. It
  reads CPython's private struct layout, and that is the one thing here a
  CPython point release could break without warning.
- Test the targets that are built but never imported: linux i686, win32,
  win_arm64. Every wheel that ships should have been imported somewhere first.
- Parametrize the tests over all three ways in -- `ctypes` on the bundled
  library, `sqlite_rs.sqlite3`, and the Rust functions -- so each is covered by
  the same cases instead of its own.

### maybe

- Generate `python/sqlite_rs/__init__.pyi` with `pyo3-stub-gen` rather than
  keeping it by hand. It cannot infer the Arrow protocols, so some of it would
  stay hand-written either way.
- Move PyPI publishing to trusted publishing and drop `PYPI_API_TOKEN`.
- Tidy the scripts that vendor `sqlite`, CPython's `sqlite` wrapper and the
  typeshed stubs.
