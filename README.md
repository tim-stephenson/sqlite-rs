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

## Claude TODOs

CI speedup ideas, not yet implemented (see .github/workflows/CI.yml):

- Split the `windows`/`macos` build jobs so each per-arch test job only
  `needs:` its own wheel, not sibling architectures sharing the same matrix
  (already done for `linux-aarch64`; `windows`/`macos` still share one job
  across architectures)
- Consolidate the per-Python-version test jobs (`test-linux-x86_64`,
  `test-macos-x86_64`, etc.) into one job per platform that loops through
  all its versions sequentially, instead of one job per version -- macOS/
  Windows runners have tight concurrency limits, so many separate jobs
  queue rather than run in parallel
- Test a smaller matrix (e.g. oldest + newest supported CPython) on every
  push/PR, full matrix only on tags/releases or a nightly cron
- Cache the interpreter downloads that aren't preinstalled on the runner
  image (3.15, 3.14t, 3.15t take 30-90s each) via `actions/cache` keyed by
  version
- Add `paths-ignore` on the workflow trigger so doc-only/non-build changes
  skip the whole matrix
