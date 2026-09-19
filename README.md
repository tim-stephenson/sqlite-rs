# sqlite-rs

A drop-in superset of Python's `sqlite3` bindings, extended with Rust.


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

`bear -- cargo build` regenerates `compile_commands.json`, which clangd needs to
resolve the vendored CPython headers in `native/`.


## TODO


### high

- Use `rusqlite` which uses the vendored C `sqlite` library

### nice to have

- Add CI for building docs
- Use `pyo3-stub-gen`

- Expand pytest tests to parametrize over the three ways to interact with `sqlite` (`ctypes` on the C library, `sqlite_rs.sqlite3`, or rust functions in `sqlite_rs._core`)
- Clean up the scripts which vendor `sqlite`, the cpython `sqlite` wrapper, the `basedpyright` cpython `sqlite` type stubs
- Look for improvements in the reliability of the method used to extract the sqlite connection from the python sqlite connection in `native/`
- Extract the sqite cursor from the python sqlite cursor