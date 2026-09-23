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
| windows-latest, x64 | 2.51 / 0.80 GB | 3.61 / 1.49 GB | 9.47 / 2.81 GB |
| ubuntu-latest, x64 | 1.89 / 0.81 GB | 4.07 / 1.73 GB | 7.87 / 2.76 GB |
| macos-latest, arm64 | 3.07 / 0.60 GB | 6.50 / 1.43 GB | 22.79 / 2.74 GB |

Read those down a column, not across. A runner's absolute speed moves by up to
2x between runs — the same code has measured 1.57s and 2.87s on Windows — so
only the three modes within one run are comparable, having shared a machine.

## Inserting

`scripts/benchmark_insert.py` is the other direction: the same rows written
into a fresh database by `execute_many`, by ADBC's own bulk ingest, and by
stdlib `executemany`. Two million rows, four columns, every mode under
`journal_mode = WAL` and `synchronous = NORMAL`, on one machine:

| | seconds | rows/s | peak RSS |
| --- | --- | --- | --- |
| `sqlite_rs` | 0.68 | 2,935,441 | 0.40 GB |
| `adbc` | 0.61 | 3,253,769 | 0.44 GB |
| stdlib `executemany` | 0.97 | 2,058,237 | 1.16 GB |

Not the lopsided margin reading gives, and worth being plain about: an insert
is mostly SQLite's own work -- the B-tree, the WAL, the page cache -- where a
read is mostly turning rows into Python objects, which is the part this skips.
Against ADBC, which also binds from Arrow, there is nothing in it; ADBC is
slightly ahead here. Against stdlib the win is about 1.4x, and the memory gap
is the one that carries over: every row has to become a tuple of Python
objects before `executemany` can start.

Two things keep the comparison honest. The journal mode belongs to the file
and is set before any mode opens it; `synchronous` belongs to a connection, so
each mode sets its own -- otherwise this would measure journal modes rather
than the three paths. And stdlib is given the shape it wants, a list of tuples
built before the clock starts, so it is not charged for a conversion it would
really pay.

## Where the time goes

Every mode starts warm: the database is read once before any of them run, their
modules are imported before their clock starts, and each fetches 50k rows
through its own path first. Of what is left, essentially all of it is the read.
Building the frame costs no measurable time or memory, because nothing is
copied into it — `TEXT` and `BLOB` are built as Arrow's view layouts, which is
what polars holds strings in, so the buffers are adopted as they are.
