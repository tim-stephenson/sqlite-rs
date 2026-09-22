# sqlite-rs

[![PyPI](https://img.shields.io/pypi/v/sqlite-rs?logo=pypi&logoColor=white)](https://pypi.org/project/sqlite-rs/)
[![Python](https://img.shields.io/pypi/pyversions/sqlite-rs?logo=python&logoColor=white)](https://pypi.org/project/sqlite-rs/)
[![Downloads](https://img.shields.io/pypi/dm/sqlite-rs?logo=pypi&logoColor=white)](https://pypi.org/project/sqlite-rs/)
[![License](https://img.shields.io/github/license/tim-stephenson/sqlite-rs)](https://github.com/tim-stephenson/sqlite-rs/blob/main/LICENSE)
[![Stars](https://img.shields.io/github/stars/tim-stephenson/sqlite-rs?logo=github)](https://github.com/tim-stephenson/sqlite-rs/stargazers)

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

# Every result column, named, and copied into polars nowhere.
table = sqlite_rs.execute_and_fetch_table(conn, "SELECT * FROM t")
pl.DataFrame(table)
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

### Vendoring

`vendor/` holds third-party source, pulled by these and never edited by hand.
Each is idempotent: re-running one with no arguments rewrites what is already
pinned, so a clean `git status` afterwards means the tree and the pin agree.

| | |
| --- | --- |
| SQLite | `uv run python scripts/vendor_sqlite.py [VERSION]` |
| CPython's `sqlite3` | `uv run python scripts/vendor_cpython.py [VERSION ...]` |
| typeshed stubs | `uv run python scripts/vendor_typeshed_sqlite3.py` |

`vendor_sqlite.py` with no argument re-vendors whatever `vendor/sqlite/VERSION`
pins; with one (`3.53.4`) it pins and vendors that instead. The download URL
and its checksum come from sqlite.org's own machine-readable product data, so
bumping a version never means editing the script.

`vendor_cpython.py` keeps a subtree per minor version, because the vendored
`Modules/_sqlite/*.c` reach into CPython internals that are not stable between
them. Given versions (`3.14.3`) it vendors exactly those, leaving the rest
alone. Given none it takes the five most recent minor versions that have a
final release -- which is not necessarily the set this project supports, and
will quietly drop the oldest once a new one lands, so pass them explicitly
unless you mean to move the floor.

`vendor_typeshed_sqlite3.py` takes no arguments. It lifts the `sqlite3` stubs
out of the installed basedpyright, recording which version and which typeshed
commit in `vendor/typeshed/MANIFEST`, so the stubs the type checker uses for
`sqlite_rs.sqlite3` are the ones it ships for the real thing.

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
