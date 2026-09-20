# Performance

`scripts/benchmark_fetch_all.py` reads a whole table into a polars `DataFrame`
three ways: through `sqlite_rs`, through ADBC's SQLite driver — the near
neighbour, which also returns Arrow, from its own bundled SQLite — and through
stdlib `sqlite3`. It needs a release build; `sqlite_rs.DEBUG_BUILD` says which
you have.

```console
$ uv sync --group bench
$ uv run maturin develop --uv --release
$ uv run python scripts/benchmark_fetch_all.py --rows 10_000_000
```

Ten million rows of a four-column `STRICT` table, on one machine:

| | seconds | peak RSS |
| --- | --- | --- |
| `sqlite_rs` | 0.94 | 0.48 GB |
| `adbc` | 2.23 | 1.58 GB |
| stdlib `fetchall()` | 4.28 | 2.56 GB |

stdlib is let off lightest of the three: `fetchall()` is where it stops, so it
is not charged for arranging its rows into anything. ADBC is given a batch size
worth having rather than its 1024-row default.

## Across platforms

The `Benchmark` workflow runs the same thing on CI, by hand, on one
interpreter. Seconds and peak RSS:

| | sqlite_rs | adbc | stdlib |
| --- | --- | --- | --- |
| windows-latest, x64 | 1.60 / 0.80 GB | 2.13 / 1.49 GB | 8.27 / 2.81 GB |
| ubuntu-latest, x64 | 1.85 / 0.81 GB | 4.09 / 1.74 GB | 7.86 / 2.76 GB |
| macos-latest, arm64 | 1.94 / 0.79 GB | 4.65 / 1.27 GB | 9.45 / 2.74 GB |

Read those down a column, not across. A runner's absolute speed moves by up to
2x between runs — the same code has measured 1.57s and 2.87s on Windows — so
only the three modes within one run are comparable, having shared a machine.

## Where the time goes

Every mode starts warm: the database is read once before any of them run, their
modules are imported before their clock starts, and each fetches 50k rows
through its own path first. Of what is left, essentially all of it is the read.
Building the frame costs no measurable time or memory, because nothing is
copied into it — `TEXT` and `BLOB` are built as Arrow's view layouts, which is
what polars holds strings in, so the buffers are adopted as they are.
