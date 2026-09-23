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
| macOS, arm64 | **2.21s** / 4,520,187 rows/s / 0.68 GB | 5.64s / 1,773,072 rows/s / 1.12 GB | 15.10s / 662,372 rows/s / 2.74 GB |
| Linux, x86-64 | **1.89s** / 5,281,716 rows/s / 0.81 GB | 4.15s / 2,412,059 rows/s / 1.75 GB | 8.00s / 1,249,572 rows/s / 2.76 GB |
| Windows, x86-64 | **3.14s** / 3,182,268 rows/s / 0.80 GB | 3.65s / 2,737,264 rows/s / 1.49 GB | 9.43s / 1,060,959 rows/s / 2.81 GB |

Insert: 2,000,000 rows, four columns, under `journal_mode = WAL` and
`synchronous = NORMAL`. Timed from connect to close, so every mode is charged
for its own commit, and each one's rows are counted back out of the database
afterwards so that nothing is credited for work it did not keep.

| | sqlite_rs | adbc | stdlib `sqlite3` |
| --- | --- | --- | --- |
| macOS, arm64 | **1.29s** / 1,545,490 rows/s / 0.40 GB | 1.65s / 1,209,644 rows/s / 0.45 GB | 2.21s / 903,370 rows/s / 1.32 GB |
| Linux, x86-64 | **1.43s** / 1,394,698 rows/s / 0.42 GB | 1.49s / 1,342,732 rows/s / 0.47 GB | 1.93s / 1,038,702 rows/s / 1.30 GB |
| Windows, x86-64 | **3.15s** / 635,307 rows/s / 0.35 GB | 3.59s / 557,014 rows/s / 0.39 GB | 4.40s / 454,816 rows/s / 1.20 GB |

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
