# sqlite-rs


## TODO

- Use `pyo3-stub-gen`
- Use `rusqlite` which uses the vendored C `sqlite` library
- (Fix) CI for building wheels
- Add CI for building docs
- Add CI for pytest tests
- Expand pytest tests to parametrize over the three ways to interact with `sqlite` (`ctypes` on the C library, `sqlite_rs.sqlite3`, or rust functions in `sqlite_rs._core`)
- Clean up the scripts which vendor `sqlite`, the cpython `sqlite` wrapper, the `basedpyright` cpython `sqlite` type stubs
- Look for improvements in the reliability of the method used to extract the sqlite connection from the python sqlite connection in `native/`
- Extract the sqite cursor from the python sqlite cursor
