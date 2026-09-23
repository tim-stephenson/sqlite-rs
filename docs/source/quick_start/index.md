# Quick start

```console
$ pip install sqlite-rs
```

## Open a connection

`sqlite_rs.sqlite3` is CPython's `sqlite3`, so this is the code you already
write:

```python
import sqlite_rs.sqlite3

conn = sqlite_rs.sqlite3.connect("example.db")
conn.execute("CREATE TABLE measurement (station TEXT, reading REAL)")
conn.executemany(
    "INSERT INTO measurement VALUES (?, ?)",
    [("hare", 12.5), ("tortoise", 9.0), ("hare", 14.25)],
)
conn.commit()
```

## Read it as columns

`execute_and_fetch_all` runs a statement on that same connection and returns
one Arrow array per result column:

```python
import sqlite_rs

station, reading = sqlite_rs.execute_and_fetch_all(
    conn, "SELECT station, reading FROM measurement"
)
len(station)  # 3
reading[1].as_py()  # 9.0
```

## Hand it to a dataframe

Each column carries its own name, so polars names the `Series` itself:

```python
import polars as pl

columns = sqlite_rs.execute_and_fetch_all(conn, "SELECT * FROM measurement")
frame = pl.DataFrame([pl.Series(c) for c in columns])
frame.columns  # ['station', 'reading']
```

Or take the whole result in one call with `execute_and_fetch_table`:

```python
pl.DataFrame(sqlite_rs.execute_and_fetch_table(conn, "SELECT * FROM measurement"))
```

Both are the same columns; the table is just all of them behind one object, for
consumers that would rather take a table than assemble one. Neither copies:
polars adopts the buffers as they are.

## Write a table back

`execute_many` is the other direction: it runs one statement once per row of
an Arrow table, which is what `INSERT`, `UPDATE` and upsert all are.

```python
import pyarrow as pa

readings = pa.table({"station": ["hare", "tortoise"], "reading": [15.0, 8.5]})
sqlite_rs.execute_many(conn, "INSERT INTO measurement VALUES (?, ?)", readings)  # 2
```

Anything that exports a table over the PyCapsule interface works -- a polars
`DataFrame`, a pyarrow `Table`, or what `execute_and_fetch_table` gave you.

Parameters bind by position for `?` and by name for `:name`, whichever the
statement uses. Naming lets the columns arrive in any order:

```python
sqlite_rs.execute_many(
    conn,
    "UPDATE measurement SET reading = :reading WHERE station = :station",
    pa.table({"reading": [16.0], "station": ["hare"]}),
)  # 1, the rows it changed
```

It returns the number of rows the statement changed, which for an `UPDATE` or
an upsert is not necessarily the number of rows you gave it.

Everything runs inside one savepoint, so a failure part-way leaves the
database as it was -- whether or not you had a transaction open already -- and
the error says which row.

## Read a cursor you already have

If you are midway through a cursor, `fetch_all` drains the rest of it — the
same statement, so the two share position:

```python
cursor = conn.execute("SELECT * FROM measurement")
cursor.fetchone()  # ('hare', 12.5), the DB-API way
station, reading = sqlite_rs.fetch_all(cursor)  # the remaining two rows
len(station)  # 2
```

## What comes back

One array per result column, each as long as the number of rows. SQLite types
values per row rather than per column, so a column is accumulated into one
typed buffer on the assumption the type it has been seeing is the type that
will keep coming. A wider value promotes what has accumulated, once, along
SQLite's own storage class order:

```text
NULL  <  INTEGER  <  REAL     <  TEXT       <  BLOB
null     int64      float64      utf8_view     binary_view
```

A NULL never promotes anything; it only contributes a null slot. In a `STRICT`
table, or any column that holds one storage class, promotion never happens at
all.

Going the other way, `execute_many` casts each column once into whichever
storage class it belongs in:

```text
NULL      INTEGER                     REAL             TEXT        BLOB
null      int8/16/32/64               float16/32/64    utf8        binary
          uint8/16/32                                  largeutf8   largebinary
          boolean                                      utf8view    binaryview
                                                                   fixedsizebinary
```

A dictionary column binds as whatever it encodes. `uint64` is deliberately
not accepted: SQLite's integers are signed, so anything above `2**63 - 1`
could only be stored by losing either its value or its type, and you know
which of those you want better than this does -- cast it yourself. Temporal
and nested types are refused for the same reason: SQLite has three competing
conventions for a timestamp and no way to record which one you meant.
