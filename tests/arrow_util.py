"""Read columnar results without depending on any Arrow package.

sqlite_rs returns arrays that speak the Arrow PyCapsule interface. Consumers
normally hand those to pyarrow or polars, but this suite deliberately installs
neither: a test-only Arrow dependency would gate which interpreters the project
can test itself against, which is exactly what dropping pyarrow fixed.

So the type is read straight out of the `ArrowSchema` struct behind
`__arrow_c_schema__`, which doubles as a check that the capsule interface
really works, and values come from the arrays' own indexing.
"""

import ctypes
from collections.abc import Sequence
from typing import ClassVar, Protocol, cast, final

# Arrow C data interface format strings, for the types sqlite_rs produces.
# https://arrow.apache.org/docs/format/CDataInterface.html#data-type-description-format-strings
_FORMATS = {"n": "null", "l": "int64", "g": "float64", "u": "utf8", "z": "binary"}


@final
class _ArrowSchema(ctypes.Structure):
    _fields_: ClassVar = (
        ("format", ctypes.c_char_p),
        ("name", ctypes.c_char_p),
        ("metadata", ctypes.c_char_p),
        ("flags", ctypes.c_int64),
        ("n_children", ctypes.c_int64),
        ("children", ctypes.c_void_p),
        ("dictionary", ctypes.c_void_p),
        ("release", ctypes.c_void_p),
        ("private_data", ctypes.c_void_p),
    )


class ArrowScalar(Protocol):
    def as_py(self) -> object: ...


class ArrowArray(Protocol):
    def __arrow_c_schema__(self) -> object: ...
    def __len__(self) -> int: ...
    def __getitem__(self, i: int) -> ArrowScalar: ...


def _read_schema(array: ArrowArray) -> tuple[str, str]:
    """(format, name), copied out while the capsule is still alive.

    The capsule owns the ArrowSchema and releases it when collected, so
    anything read from the struct has to be copied before it goes out of
    scope -- holding a ctypes view past that point reads freed memory.
    """
    capsule = array.__arrow_c_schema__()
    get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    get_pointer.restype = ctypes.c_void_p
    get_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    pointer = cast("int", get_pointer(capsule, b"arrow_schema"))
    schema = ctypes.cast(pointer, ctypes.POINTER(_ArrowSchema)).contents
    # ctypes struct fields are untyped; both are `const char*`.
    raw_format = cast("bytes | None", schema.format)
    raw_name = cast("bytes | None", schema.name)
    # .decode() copies, so `capsule` need only live until here.
    result = (
        raw_format.decode() if raw_format else "",
        raw_name.decode() if raw_name else "",
    )
    del capsule
    return result


def dtype(array: ArrowArray) -> str:
    """Return the Arrow type as a readable name, via the exported schema."""
    fmt, _ = _read_schema(array)
    return _FORMATS.get(fmt, fmt)


def name(array: ArrowArray) -> str:
    """Return the column name carried in the exported schema."""
    return _read_schema(array)[1]


def values(array: ArrowArray) -> list[object]:
    return [array[i].as_py() for i in range(len(array))]


def columns(arrays: Sequence[ArrowArray]) -> list[list[object]]:
    """Just the values, column by column."""
    return [values(a) for a in arrays]


def rows(arrays: Sequence[ArrowArray]) -> list[list[object]]:
    """Columnar back to row-major, for comparing against SQL results."""
    if not arrays:
        return []
    return [list(row) for row in zip(*(values(a) for a in arrays), strict=True)]
