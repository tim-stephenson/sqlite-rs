# sqlite-rs

[![PyPI](https://img.shields.io/pypi/v/sqlite-rs?logo=pypi&logoColor=white)](https://pypi.org/project/sqlite-rs/)
[![Python](https://img.shields.io/pypi/pyversions/sqlite-rs?logo=python&logoColor=white)](https://pypi.org/project/sqlite-rs/)
[![Downloads](https://img.shields.io/pypi/dm/sqlite-rs?logo=pypi&logoColor=white)](https://pypi.org/project/sqlite-rs/)
[![Platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey)](https://pypi.org/project/sqlite-rs/#files)
[![Docs](https://img.shields.io/github/deployments/tim-stephenson/sqlite-rs/github-pages?label=docs&logo=readthedocs&logoColor=white)](https://tim-stephenson.github.io/sqlite-rs/)
[![License](https://img.shields.io/github/license/tim-stephenson/sqlite-rs)](https://github.com/tim-stephenson/sqlite-rs/blob/main/LICENSE)
[![Stars](https://img.shields.io/github/stars/tim-stephenson/sqlite-rs?logo=github)](https://github.com/tim-stephenson/sqlite-rs/stargazers)

A drop-in superset of Python's `sqlite3` bindings, extended with Rust for
performant Arrow interoperability.

## Performance

Query: 10,000,000 rows, four columns, one `SELECT *`.

| | sqlite_rs | adbc | stdlib `sqlite3` |
| --- | --- | --- | --- |
| macOS, arm64 | **3.77s** / 2,654,184 rows/s / 0.64 GB | 9.69s / 1,032,200 rows/s / 1.36 GB | 21.94s / 455,860 rows/s / 2.74 GB |
| Linux, x86-64 | **1.79s** / 5,592,085 rows/s / 0.81 GB | 4.34s / 2,302,589 rows/s / 1.73 GB | 9.01s / 1,109,456 rows/s / 2.76 GB |
| Windows, x86-64 | **2.42s** / 4,125,213 rows/s / 0.80 GB | 3.54s / 2,824,285 rows/s / 1.49 GB | 9.49s / 1,053,537 rows/s / 2.81 GB |

Insert: 2,000,000 rows, four columns, under `journal_mode = WAL` and
`synchronous = NORMAL`. Timed from connect to close, so each mode is charged
for its own commit and checkpoint.

| | sqlite_rs | adbc | stdlib `sqlite3` |
| --- | --- | --- | --- |
| macOS, arm64 | 1.33s / 1,505,093 rows/s / 0.40 GB | **1.30s** / 1,538,710 rows/s / 0.45 GB | 2.36s / 847,610 rows/s / 1.32 GB |
| Linux, x86-64 | 1.32s / 1,516,108 rows/s / 0.42 GB | **1.20s** / 1,670,887 rows/s / 0.47 GB | 1.85s / 1,079,033 rows/s / 1.30 GB |
| Windows, x86-64 | 3.51s / 569,525 rows/s / 0.36 GB | **1.70s** / 1,178,428 rows/s / 0.39 GB | 4.89s / 408,871 rows/s / 1.20 GB |

Reads are where the Arrow path pays off. Inserts are a closer thing: ADBC edges
it on every platform, and on Windows it is twice as quick, which is not yet
understood.

## Opinionated Installation Choices

- `sqlite_rs` bundles a vendored dynamically linkable SQLite library. (`libsqlite_rs_sqlite3.so`, `libsqlite_rs_sqlite3.dylib`, or `libsqlite_rs_sqlite3.dll`)
- `sqlite_rs` bundles a vendored cpython `sqlite3` module (matching the python version) which dynamically links to the bundled SQLite library.
- `sqlite_rs` bundles functions with native performance and Arrow interoperability which dynamically links to the bundled SQLite library, utilizing the known struct offsets from the vendored `sqlite3` (e.g. `sqlite_rs.sqlite3`) to extract the relevant SQLite references.
- `sqlite_rs` builds that SQLite in multi-thread rather than serialized mode, which is worth roughly 15% on queries and 12% on inserts. Threads may share the module but not a single connection, so `sqlite_rs.sqlite3.threadsafety` is `1` where the standard library reports `3`, and a connection must not be handed between threads even with `check_same_thread=False`.

## Quick Start

`sqlite_rs.sqlite3` is a drop in replacement for the built in `sqlite3` library,
because it is, line for line, the exact same code, vendored from the cpython library.
However, using `sqlite_rs.sqlite3` instead allows passing a `Connection` or `Cursor`
to the blazing fast Arrow interoperable functions apart of `sqlite_rs`.

```python
import polars as pl
import sqlite_rs
import sqlite_rs.sqlite3 as sqlite3

conn = sqlite3.connect("example.db")
conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
conn.execute("INSERT INTO t VALUES (1, 'x')")

table = pl.DataFrame(sqlite_rs.execute_and_fetch_table(conn, "SELECT * FROM t"))
```


## AI policy

Almost all of the code in this repository was written by an LLM. A human directed it
throughout: the higher-level structure and the design choices are theirs, and
the LLM wrote the code that followed from them. This documentation was written
the same way.


## Development

| | |
| --- | --- |
| build | `uv run maturin develop --uv` |
| build (release) | `uv run maturin develop --uv --release` |
| test | `uv run pytest tests/` |
| lint | `uv run ruff check . && uv run ruff format --check .` |
| types | `uv run basedpyright . --warnings` |
| docs | `uv run sphinx-autobuild docs/source docs/build` |
| bench (read) | `uv run python scripts/benchmark_fetch_all.py` |
| bench (insert) | `uv run python scripts/benchmark_insert.py` |
| clangd | `bear -- cargo build` |
| vendor SQLite | `uv run python scripts/vendor_sqlite.py [VERSION]` |
| vendor CPython's `sqlite3` | `uv run python scripts/vendor_cpython.py [VERSION ...]` |
| vendor basedpyright stubs | `uv run python scripts/vendor_basedpyright_stubs.py` |
