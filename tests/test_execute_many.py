# pyright: reportAny=false, reportExplicitAny=false
# pyarrow is not a declared dependency -- see the module docstring -- so
# everything reached through it is untyped here.
"""Tests for inserting Arrow data with `execute_many`.

The spine is a round trip: put an Arrow column in, read it back with
`execute_and_fetch_all`, and check both the storage class SQLite chose and the
values. The table column is left untyped so nothing has an affinity to coerce
through -- what comes back is what this bound, not what the schema wanted.

Constructing the more exotic Arrow types needs a library that can build them,
which is pyarrow here. It has no wheels for the two 32-bit targets, so those
runs skip this module rather than fail; everything else covers it.
"""

from typing import Any

import arrow_util as au
import pytest
import sqlite_rs
import sqlite_rs.sqlite3

pa = pytest.importorskip("pyarrow")


def _conn() -> sqlite_rs.sqlite3.Connection:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (v)")
    return conn


def _round_trip(column: Any) -> tuple[str, list[object]]:  # noqa: ANN401
    """Insert one column, read it back, and report (arrow type, values)."""
    conn = _conn()
    inserted = sqlite_rs.execute_many(
        conn, "INSERT INTO t VALUES (?)", pa.table({"v": column})
    )
    assert inserted == len(column)
    [read] = sqlite_rs.execute_and_fetch_all(conn, "SELECT v FROM t")
    return au.dtype(read), au.values(read)


@pytest.mark.parametrize(
    ("arrow_type", "values", "expected_type", "expected"),
    [
        # Every integer width SQLite can hold losslessly, signed and not.
        ("int8", [-128, 127], "int64", [-128, 127]),
        ("int16", [-32768, 32767], "int64", [-32768, 32767]),
        ("int32", [-2147483648, 2147483647], "int64", [-2147483648, 2147483647]),
        ("int64", [-(2**63), 2**63 - 1], "int64", [-(2**63), 2**63 - 1]),
        ("uint8", [0, 255], "int64", [0, 255]),
        ("uint16", [0, 65535], "int64", [0, 65535]),
        ("uint32", [0, 4294967295], "int64", [0, 4294967295]),
        # Exact past 2**53, which going through REAL would not be.
        ("int64", [2**53 + 1], "int64", [2**53 + 1]),
        ("bool_", [True, False], "int64", [1, 0]),
        ("float32", [1.5, -2.25], "float64", [1.5, -2.25]),
        ("float64", [1.5, -2.25], "float64", [1.5, -2.25]),
        ("string", ["x", ""], "utf8_view", ["x", ""]),
        ("large_string", ["x", ""], "utf8_view", ["x", ""]),
        ("string_view", ["x", ""], "utf8_view", ["x", ""]),
        ("binary", [b"\x00\xff", b""], "binary_view", [b"\x00\xff", b""]),
        ("large_binary", [b"\x00\xff", b""], "binary_view", [b"\x00\xff", b""]),
        ("binary_view", [b"\x00\xff", b""], "binary_view", [b"\x00\xff", b""]),
        ("null", [None, None], "null", [None, None]),
    ],
)
def test_every_supported_type_round_trips(
    arrow_type: str, values: list[object], expected_type: str, expected: list[object]
) -> None:
    column = pa.array(values, type=getattr(pa, arrow_type)())

    assert _round_trip(column) == (expected_type, expected)


def test_float16_widens_to_real() -> None:
    # Separately, because only some values survive half precision exactly.
    assert _round_trip(pa.array([1.5, -2.25], type=pa.float16())) == (
        "float64",
        [1.5, -2.25],
    )


def test_fixed_size_binary_is_a_blob() -> None:
    column = pa.array([b"ab", b"cd"], type=pa.binary(2))

    assert _round_trip(column) == ("binary_view", [b"ab", b"cd"])


@pytest.mark.parametrize(
    ("values", "value_type", "expected_type", "expected"),
    [
        (["x", "y", "x"], pa.string(), "utf8_view", ["x", "y", "x"]),
        ([1, 2, 1], pa.int64(), "int64", [1, 2, 1]),
    ],
)
def test_a_dictionary_binds_as_what_it_encodes(
    values: list[object],
    value_type: Any,  # noqa: ANN401
    expected_type: str,
    expected: list[object],
) -> None:
    # The keys are an encoding, not a type; nothing about them should reach
    # SQLite.
    column = pa.array(values, type=value_type).dictionary_encode()

    assert _round_trip(column) == (expected_type, expected)


@pytest.mark.parametrize(
    "arrow_type",
    ["int8", "int64", "float64", "string", "binary", "bool_"],
)
def test_a_null_in_any_column_binds_as_null(arrow_type: str) -> None:
    # The validity bitmap is where binding goes wrong, so every type gets a
    # hole in the middle of it.
    column = pa.array([None], type=getattr(pa, arrow_type)())

    assert _round_trip(column) == ("null", [None])


@pytest.mark.parametrize(
    ("arrow_type", "value"),
    [("uint64", 1), ("timestamp", 0), ("list_", [1]), ("struct", {"a": 1})],
)
def test_types_with_no_storage_class_are_refused(
    arrow_type: str, value: object
) -> None:
    # uint64 deliberately among them: above i64::MAX it could only be stored
    # by losing its value or its type, and the caller knows which they want.
    builders = {
        "uint64": lambda: pa.array([value], type=pa.uint64()),
        "timestamp": lambda: pa.array([value], type=pa.timestamp("s")),
        "list_": lambda: pa.array([value], type=pa.list_(pa.int64())),
        "struct": lambda: pa.array([value], type=pa.struct([("a", pa.int64())])),
    }
    conn = _conn()

    with pytest.raises(TypeError, match="no SQLite storage class"):
        _ = sqlite_rs.execute_many(
            conn, "INSERT INTO t VALUES (?)", pa.table({"v": builders[arrow_type]()})
        )


def _pair() -> sqlite_rs.sqlite3.Connection:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    return conn


def _rows() -> Any:  # noqa: ANN401
    return pa.table({"a": [1, 2], "b": ["x", "y"]})


def test_columns_bind_by_position_for_question_marks() -> None:
    conn = _pair()

    assert sqlite_rs.execute_many(conn, "INSERT INTO t VALUES (?, ?)", _rows()) == 2  # noqa: PLR2004
    assert conn.execute("SELECT a, b FROM t").fetchall() == [(1, "x"), (2, "y")]


def test_columns_bind_by_name_for_named_parameters() -> None:
    # The parameters are listed in the opposite order to the columns. Getting
    # this right is the whole point of deciding by what the statement uses.
    conn = _pair()

    inserted = sqlite_rs.execute_many(
        conn, "INSERT INTO t (b, a) VALUES (:b, :a)", _rows()
    )

    assert inserted == 2  # noqa: PLR2004
    assert conn.execute("SELECT a, b FROM t").fetchall() == [(1, "x"), (2, "y")]


def test_named_binding_ignores_a_column_it_has_no_parameter_for() -> None:
    # Naming what you want is the point of naming.
    conn = _pair()

    inserted = sqlite_rs.execute_many(conn, "INSERT INTO t (a) VALUES (:a)", _rows())

    assert inserted == 2  # noqa: PLR2004
    assert conn.execute("SELECT a, b FROM t").fetchall() == [(1, None), (2, None)]


def test_an_update_reports_the_rows_it_changed() -> None:
    conn = _pair()
    _ = sqlite_rs.execute_many(conn, "INSERT INTO t VALUES (?, ?)", _rows())

    changed = sqlite_rs.execute_many(
        conn,
        "UPDATE t SET b = :b WHERE a = :a",
        pa.table({"a": [1, 2, 99], "b": ["X", "Y", "Z"]}),
    )

    # Three rows given, two of them matching something.
    assert changed == 2  # noqa: PLR2004
    assert conn.execute("SELECT b FROM t ORDER BY a").fetchall() == [("X",), ("Y",)]


def test_an_upsert_inserts_then_updates() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE k (a INTEGER PRIMARY KEY, b TEXT)")

    changed = sqlite_rs.execute_many(
        conn,
        "INSERT INTO k VALUES (:a, :b) ON CONFLICT(a) DO UPDATE SET b = :b",
        pa.table({"a": [1, 1], "b": ["first", "second"]}),
    )

    assert changed == 2  # noqa: PLR2004
    assert conn.execute("SELECT a, b FROM k").fetchall() == [(1, "second")]


def test_the_same_rows_in_several_batches_are_the_same_insert() -> None:
    one = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    three = pa.Table.from_batches(one.to_batches(max_chunksize=1))
    assert len(three.to_batches()) == 3  # noqa: PLR2004

    whole, split = _pair(), _pair()
    assert sqlite_rs.execute_many(whole, "INSERT INTO t VALUES (?, ?)", one) == 3  # noqa: PLR2004
    assert sqlite_rs.execute_many(split, "INSERT INTO t VALUES (?, ?)", three) == 3  # noqa: PLR2004

    assert (
        whole.execute("SELECT a, b FROM t").fetchall()
        == split.execute("SELECT a, b FROM t").fetchall()
    )


def test_no_rows_changes_nothing_but_still_checks_the_sql() -> None:
    empty = pa.table({"a": pa.array([], pa.int64()), "b": pa.array([], pa.string())})

    assert sqlite_rs.execute_many(_pair(), "INSERT INTO t VALUES (?, ?)", empty) == 0

    # The statement is prepared either way, so a bad one is still an error.
    with pytest.raises(ValueError, match="no such table"):
        _ = sqlite_rs.execute_many(_pair(), "INSERT INTO nope VALUES (?, ?)", empty)


def test_a_failure_part_way_leaves_nothing_behind() -> None:
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("CREATE TABLE u (a INTEGER PRIMARY KEY)")
    _ = conn.execute("INSERT INTO u VALUES (2)")

    with pytest.raises(ValueError, match=r"row 1: .*UNIQUE"):
        _ = sqlite_rs.execute_many(
            conn, "INSERT INTO u VALUES (?)", pa.table({"a": [1, 2, 3]})
        )

    # Row 0 went in before row 1 failed; the savepoint took it back out.
    assert conn.execute("SELECT a FROM u").fetchall() == [(2,)]


def test_it_nests_inside_a_transaction_the_caller_opened() -> None:
    conn = _pair()
    _ = conn.execute("BEGIN")

    inserted = sqlite_rs.execute_many(conn, "INSERT INTO t VALUES (?, ?)", _rows())

    assert inserted == 2  # noqa: PLR2004
    assert conn.in_transaction
    # The savepoint released into the caller's transaction, not past it.
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (0,)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        # SQLite rejects a VALUES list of the wrong length itself, so these
        # are statements it accepts and only the column count is wrong.
        (
            "INSERT INTO t (a, b) VALUES (?, ? || ?)",
            r"parameters \(3\) and columns \(2\)",
        ),
        ("INSERT INTO t (a) VALUES (?)", r"parameters \(1\) and columns \(2\)"),
        ("INSERT INTO t VALUES (:a, :zz)", "parameter :zz has no column"),
        ("INSERT INTO t VALUES (?, :b)", "mixes named and positional"),
        ("INSERT INTO t VALUES (?, ?); SELECT 1", "single statement"),
        ("INSERT INTO nope VALUES (?, ?)", "no such table"),
    ],
)
def test_statements_that_do_not_fit_the_data_are_refused(
    sql: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _ = sqlite_rs.execute_many(_pair(), sql, _rows())


def test_it_refuses_a_connection_from_the_stdlib_module() -> None:
    import sqlite3  # noqa: PLC0415

    stdlib = sqlite3.connect(":memory:")
    _ = stdlib.execute("CREATE TABLE t (a INTEGER, b TEXT)")

    with pytest.raises(TypeError, match=r"sqlite_rs\.sqlite3\.connect"):
        _ = sqlite_rs.execute_many(
            stdlib,  # pyright: ignore[reportArgumentType]
            "INSERT INTO t VALUES (?, ?)",
            _rows(),
        )


def test_a_raw_pointer_reaches_the_same_connection() -> None:
    conn = _pair()

    inserted = sqlite_rs.execute_many_via_raw_pointer(
        sqlite_rs.get_raw_db_ptr(conn), "INSERT INTO t VALUES (?, ?)", _rows()
    )

    assert inserted == 2  # noqa: PLR2004
    assert conn.execute("SELECT a, b FROM t").fetchall() == [(1, "x"), (2, "y")]
