#!/usr/bin/env python3
"""Vendor basedpyright's typeshed sqlite3/_sqlite3 stubs, unmodified, into vendor/typeshed/.

Usage:
    python scripts/vendor_typeshed_sqlite3.py

Sourced from the `basedpyright` package installed in this project's dev
dependency group (see pyproject.toml) -- its bundled typeshed-fallback is
the exact stub content the "basedpyright" type checker itself uses for the
real stdlib `sqlite3`/`_sqlite3`, so this is how sqlite_rs.sqlite3 ends up
typed identically to it.

Like scripts/vendor_cpython.py, files are copied byte-for-byte, preserving
typeshed's own layout (`stdlib/_sqlite3.pyi`, `stdlib/sqlite3/*.pyi`) --
nothing is patched or rewritten here. build.rs's `materialize_typeshed_sqlite3`
mechanically rewrites the small number of self-referential absolute imports
these stubs carry when it copies them into python/sqlite_rs/sqlite3/, the
same as it already does for the vendored runtime .py files (see
`LIB_SQLITE3_IMPORT_REWRITES` in build.rs). A MANIFEST recording the
basedpyright version and upstream typeshed commit is written alongside for
provenance.
"""  # noqa: E501

from __future__ import annotations

import shutil
from importlib.metadata import version
from pathlib import Path

import basedpyright  # pyright: ignore[reportMissingTypeStubs]

TYPESHED_STDLIB = (
    Path(basedpyright.__file__).parent / "dist" / "typeshed-fallback" / "stdlib"
)
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "typeshed"
DEST_STDLIB = VENDOR_DIR / "stdlib"
MANIFEST_FILE = VENDOR_DIR / "MANIFEST"

# Paths relative to TYPESHED_STDLIB / DEST_STDLIB -- identical on both sides,
# preserving typeshed's own layout.
FILES = [
    "_sqlite3.pyi",
    "sqlite3/__init__.pyi",
    "sqlite3/dbapi2.pyi",
    "sqlite3/dump.pyi",
]


def main() -> None:
    commit = (TYPESHED_STDLIB.parent / "commit.txt").read_text().strip()

    for relative_path in FILES:
        dest = DEST_STDLIB / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(TYPESHED_STDLIB / relative_path, dest)
        print(f"wrote {dest}")

    _ = MANIFEST_FILE.write_text(
        f"""source: basedpyright=={version("basedpyright")}
typeshed commit: {commit}
files: {", ".join(FILES)}
""",
    )
    print(f"wrote {MANIFEST_FILE}")


if __name__ == "__main__":
    main()
