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

Query:

| | sqlite_rs | adbc | stdlib `sqlite3` |
| --- | --- | --- | --- |
| macOS, arm64 | **1.94s** / 5,161,686 rows/s / 0.79 GB | 4.65s / 2,151,119 rows/s / 1.27 GB | 9.45s / 1,057,731 rows/s / 2.74 GB |
| Linux, x86-64 | **1.85s** / 5,403,427 rows/s / 0.81 GB | 4.09s / 2,446,516 rows/s / 1.74 GB | 7.86s / 1,271,890 rows/s / 2.76 GB |
| Windows, x86-64 | **1.60s** / 6,267,934 rows/s / 0.80 GB | 2.13s / 4,702,584 rows/s / 1.49 GB | 8.27s / 1,208,718 rows/s / 2.81 GB |

## Opinionated Installation Choices

- `sqlite_rs` bundles a vendored dynamically linkable SQLite library. (`libsqlite_rs_sqlite3.so`, `libsqlite_rs_sqlite3.dylib`, or `libsqlite_rs_sqlite3.dll`)
- `sqlite_rs` bundles a vendored cpython `sqlite3` module (matching the python version) which dynamically links to the bundled SQLite library.
- `sqlite_rs` bundles functions with native performance and Arrow interoperability which dynamically links to the bundled SQLite library, utilizing the known struct offsets from the vendored `sqlite3` (e.g. `sqlite_rs.sqlite3`) to extract the relevant SQLite references.

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
| bench | `uv run python scripts/benchmark_fetch_all.py` |
| clangd | `bear -- cargo build` |
| vendor SQLite | `uv run python scripts/vendor_sqlite.py [VERSION]` |
| vendor CPython's `sqlite3` | `uv run python scripts/vendor_cpython.py [VERSION ...]` |
| vendor basedpyright stubs | `uv run python scripts/vendor_typeshed_sqlite3.py` |
