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
