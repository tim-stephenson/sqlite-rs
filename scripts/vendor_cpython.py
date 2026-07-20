#!/usr/bin/env python3
"""Vendor CPython's sqlite-related sources, unmodified, into vendor/cpython/.

Usage:
    python scripts/vendor_cpython.py [VERSION]

With no argument, re-vendors whatever version is currently pinned in
vendor/cpython/VERSION. With a version argument (e.g. "3.14.3"), pins and
vendors that version's `v<VERSION>` tag instead, overwriting VERSION.

Two subtrees are pulled byte-for-byte from github.com/python/cpython at the
pinned tag -- nothing here is patched or regenerated:

- Modules/_sqlite/ (including the clinic/ subdirectory, whose generated
  headers the .c files #include): the C extension, compiled by build.rs.
- Lib/sqlite3/{__init__,dbapi2,dump}.py (not __main__.py, the `python -m
  sqlite3` CLI, which sqlite_rs has no use for): the DB-API 2.0 wrapper
  around the C extension. build.rs copies these into
  python/sqlite_rs/sqlite3/, mechanically rewriting their few
  self-referential absolute imports (`from sqlite3.dbapi2 import ...` etc.)
  to point at that nested location -- see build.rs's
  `rewrite_self_imports`, which uses the exact same substitutions as
  scripts/vendor_typeshed_sqlite3.py applies to the corresponding stubs.

A MANIFEST recording the resolved commit SHA is written alongside for
provenance.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import cast

REPO = "python/cpython"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "cpython"
VERSION_FILE = VENDOR_DIR / "VERSION"
MANIFEST_FILE = VENDOR_DIR / "MANIFEST"

# (subpath under the repo, dest dir under VENDOR_DIR, filenames to keep or
# None for "everything, recursively")
SUBTREES = [
    ("Modules/_sqlite", "Modules/_sqlite", None),
    ("Lib/sqlite3", "Lib/sqlite3", {"__init__.py", "dbapi2.py", "dump.py"}),
]


def api_get(url: str) -> object:
    """GET a GitHub API URL and return the parsed JSON body."""
    if not url.startswith(("http:", "https:")):
        msg = "URL must start with 'http:' or 'https:'"
        raise ValueError(msg)
    request = urllib.request.Request(  # noqa: S310
        url, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request) as resp:  # noqa: S310
        return json.loads(resp.read())


def resolve_commit(tag: str) -> str:
    """Resolve a tag (or any ref) to the commit SHA it points at."""
    data = api_get(f"https://api.github.com/repos/{REPO}/commits/{tag}")
    return cast("dict[str, str]", data)["sha"]


def clear_dir(dest: Path) -> None:
    if not dest.exists():
        return
    for path in sorted(dest.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
    for path in sorted(dest.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()


def download_tree(ref: str, subpath: str, dest: Path, keep: set[str] | None) -> None:
    """Download `subpath` at `ref` into `dest`, preserving layout.

    Recurses into subdirectories unconditionally; `keep`, if given, filters
    which *files* (not directories) at this level are downloaded.
    """
    entries = api_get(
        f"https://api.github.com/repos/{REPO}/contents/{subpath}?ref={ref}"
    )
    dest.mkdir(parents=True, exist_ok=True)
    for entry in cast("list[dict[str, str]]", entries):
        if entry["type"] == "dir":
            download_tree(ref, entry["path"], dest / entry["name"], keep=None)
        elif entry["type"] == "file":
            if keep is not None and entry["name"] not in keep:
                continue
            with urllib.request.urlopen(entry["download_url"]) as resp:  # noqa: S310
                (dest / entry["name"]).write_bytes(resp.read())
        else:
            msg = f"unexpected entry type {entry['type']!r} for {entry['path']}"
            raise SystemExit(msg)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        nargs="?",
        help="CPython version to vendor, e.g. 3.14.3. Defaults to the version in VERSION.",  # noqa: E501
    )
    args = parser.parse_args()

    version = args.version
    if version is None:
        if not VERSION_FILE.exists():
            sys.exit("no version given and vendor/cpython/VERSION does not exist yet")
        version = VERSION_FILE.read_text().strip()

    tag = f"v{version}"
    print(f"resolving {REPO}@{tag}")
    commit = resolve_commit(tag)

    for source_subpath, dest_subpath, keep in SUBTREES:
        dest_root = VENDOR_DIR / dest_subpath
        print(f"downloading {source_subpath}/ from {REPO}@{commit}")
        clear_dir(dest_root)
        download_tree(commit, source_subpath, dest_root, keep)
        print(f"wrote {dest_root}")

    VERSION_FILE.write_text(version + "\n")
    MANIFEST_FILE.write_text(
        f"source: https://github.com/{REPO}\n"
        f"tag: {tag}\n"
        f"commit: {commit}\n"
        + "".join(
            f"path: {source_subpath}"
            + (f" (files: {', '.join(sorted(keep))})" if keep else "")
            + "\n"
            for source_subpath, _, keep in SUBTREES
        ),
    )
    print(f"wrote {MANIFEST_FILE}")


if __name__ == "__main__":
    main()
