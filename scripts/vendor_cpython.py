#!/usr/bin/env python3
"""Vendor CPython's sqlite-related sources, unmodified, into vendor/cpython/.

Usage:
    python scripts/vendor_cpython.py [VERSION ...]

With no arguments, auto-discovers the --count (default 5) most recent
CPython minor versions that have at least one final release, resolves each
to its latest patch release, and (re-)vendors all of them. With one or more
explicit VERSION arguments (e.g. "3.14.3"), vendors exactly those versions
instead -- each pinned into the subdirectory for its own minor version,
leaving any other already-vendored minor versions untouched.

Every vendored minor version lives in its own `vendor/cpython/<major>.<minor>/`
subdirectory (e.g. `vendor/cpython/3.14/`), because build.rs picks which one
to compile against based on the target interpreter's own version -- the
vendored `Modules/_sqlite/*.c` sources reach into CPython's internal
(`pycore_*.h`) headers, which are not a stable API across minor versions.

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

A MANIFEST recording the resolved commit SHA is written alongside each
vendored minor version, for provenance.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import cast

REPO = "python/cpython"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "cpython"

# (subpath under the repo, dest dir under the version's vendor root, filenames
# to keep or None for "everything, recursively")
SUBTREES = [
    ("Modules/_sqlite", "Modules/_sqlite", None),
    ("Lib/sqlite3", "Lib/sqlite3", {"__init__.py", "dbapi2.py", "dump.py"}),
]

RELEASE_TAG_RE = re.compile(r"^refs/tags/v3\.(\d+)\.(\d+)$")


def api_get(url: str) -> object:
    """GET a GitHub API URL and return the parsed JSON body."""
    if not url.startswith(("http:", "https:")):
        msg = "URL must start with 'http:' or 'https:'"
        raise ValueError(msg)
    request = urllib.request.Request(  # noqa: S310
        url, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request) as resp:  # noqa: S310  # pyright: ignore[reportAny]
        return json.loads(resp.read())  # pyright: ignore[reportAny]


def resolve_commit(tag: str) -> str:
    """Resolve a tag (or any ref) to the commit SHA it points at."""
    data = api_get(f"https://api.github.com/repos/{REPO}/commits/{tag}")
    return cast("dict[str, str]", data)["sha"]


def discover_latest_versions(count: int) -> dict[str, str]:
    """Return the `count` most recent CPython 3.x minor versions.

    Maps "3.<minor>" to "3.<minor>.<latest patch>" for each. Skips
    alpha/beta/rc/pre-release tags, which don't match RELEASE_TAG_RE.
    """
    refs = api_get(f"https://api.github.com/repos/{REPO}/git/matching-refs/tags/v3.")
    latest_patch: dict[int, int] = {}
    for entry in cast("list[dict[str, str]]", refs):
        match = RELEASE_TAG_RE.match(entry["ref"])
        if not match:
            continue
        minor, patch = int(match.group(1)), int(match.group(2))
        if patch > latest_patch.get(minor, -1):
            latest_patch[minor] = patch
    top_minors = sorted(latest_patch, reverse=True)[:count]
    return {f"3.{minor}": f"3.{minor}.{latest_patch[minor]}" for minor in top_minors}


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

    `keep`, if given, filters which *names* -- files or directories -- at
    this level are downloaded; subdirectories that pass the filter (or all
    of them, when `keep` is None) recurse with no further filtering, since
    `keep` only ever names top-level entries (e.g. some CPython versions'
    `Lib/sqlite3/` has a `test/` subdirectory alongside the three files this
    project vendors -- `keep` must exclude that whole subdirectory, not just
    filter files inside it).
    """
    entries = api_get(
        f"https://api.github.com/repos/{REPO}/contents/{subpath}?ref={ref}"
    )
    dest.mkdir(parents=True, exist_ok=True)
    for entry in cast("list[dict[str, str]]", entries):
        if keep is not None and entry["name"] not in keep:
            continue
        if entry["type"] == "dir":
            download_tree(ref, entry["path"], dest / entry["name"], keep=None)
        elif entry["type"] == "file":
            with urllib.request.urlopen(entry["download_url"]) as resp:  # noqa: S310  # pyright: ignore[reportAny]
                _ = (dest / entry["name"]).write_bytes(resp.read())  # pyright: ignore[reportAny]
        else:
            msg = f"unexpected entry type {entry['type']!r} for {entry['path']}"
            raise SystemExit(msg)


def vendor_one(minor: str, version: str) -> None:
    dest_root = VENDOR_DIR / minor
    tag = f"v{version}"
    print(f"resolving {REPO}@{tag}")
    commit = resolve_commit(tag)

    for source_subpath, dest_subpath, keep in SUBTREES:
        dest = dest_root / dest_subpath
        print(f"downloading {source_subpath}/ from {REPO}@{commit} into {dest}")
        clear_dir(dest)
        download_tree(commit, source_subpath, dest, keep)
        print(f"wrote {dest}")

    _ = (dest_root / "VERSION").write_text(version + "\n")
    manifest_file = dest_root / "MANIFEST"
    _ = manifest_file.write_text(
        f"source: https://github.com/{REPO}\ntag: {tag}\ncommit: {commit}\n"
        + "".join(
            f"path: {source_subpath}"
            + (f" (files: {', '.join(sorted(keep))})" if keep else "")
            + "\n"
            for source_subpath, _, keep in SUBTREES
        ),
    )
    print(f"wrote {manifest_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "versions",
        nargs="*",
        help="Explicit full CPython versions to vendor, e.g. 3.14.3 3.13.9. Each is vendored into the subdirectory for its own minor version. With none given, auto-discovers and vendors the --count most recent minor versions instead.",  # noqa: E501
    )
    _ = parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="How many of the most recent minor versions to auto-discover when no explicit VERSION arguments are given (default: 5).",  # noqa: E501
    )
    args = parser.parse_args()

    explicit_versions = cast("list[str]", args.versions)
    if explicit_versions:
        versions = {".".join(v.split(".")[:2]): v for v in explicit_versions}
    else:
        count = cast("int", args.count)
        print(f"discovering the {count} most recent CPython minor versions...")
        versions = discover_latest_versions(count)
        if not versions:
            sys.exit("found no CPython release tags to vendor")

    for minor, version in sorted(
        versions.items(), key=lambda kv: tuple(map(int, kv[0].split(".")))
    ):
        vendor_one(minor, version)


if __name__ == "__main__":
    main()
