#!/usr/bin/env python3
"""Vendor CPython's Modules/_sqlite/ sources, unmodified, into vendor/cpython/.

Usage:
    python scripts/vendor_cpython.py [VERSION]

With no argument, re-vendors whatever version is currently pinned in
vendor/cpython/VERSION. With a version argument (e.g. "3.14.3"), pins and
vendors that version's `v<VERSION>` tag instead, overwriting VERSION.

Every file under Modules/_sqlite/ (including the clinic/ subdirectory, whose
generated headers the .c files #include) is copied byte-for-byte from
github.com/python/cpython at the pinned tag -- nothing here is patched or
regenerated. A MANIFEST recording the resolved commit SHA is written
alongside for provenance.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO = "python/cpython"
SOURCE_SUBPATH = "Modules/_sqlite"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "cpython"
VERSION_FILE = VENDOR_DIR / "VERSION"
MANIFEST_FILE = VENDOR_DIR / "MANIFEST"
DEST_ROOT = VENDOR_DIR / SOURCE_SUBPATH


def api_get(url: str) -> object:
    """GET a GitHub API URL and return the parsed JSON body."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})  # noqa: S310
    with urllib.request.urlopen(request) as resp:  # noqa: S310
        return json.loads(resp.read())


def resolve_commit(tag: str) -> str:
    """Resolve a tag (or any ref) to the commit SHA it points at."""
    data = api_get(f"https://api.github.com/repos/{REPO}/commits/{tag}")
    return data["sha"]


def download_tree(ref: str, subpath: str, dest: Path) -> None:
    """Recursively download every file under `subpath` at `ref` into `dest`, preserving layout."""
    entries = api_get(f"https://api.github.com/repos/{REPO}/contents/{subpath}?ref={ref}")
    dest.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry["type"] == "dir":
            download_tree(ref, entry["path"], dest / entry["name"])
        elif entry["type"] == "file":
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
        help="CPython version to vendor, e.g. 3.14.3. Defaults to the version in VERSION.",
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

    print(f"downloading {SOURCE_SUBPATH}/ from {REPO}@{commit}")
    if DEST_ROOT.exists():
        for path in sorted(DEST_ROOT.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
        for path in sorted(DEST_ROOT.rglob("*"), reverse=True):
            if path.is_dir():
                path.rmdir()
    download_tree(commit, SOURCE_SUBPATH, DEST_ROOT)

    VERSION_FILE.write_text(version + "\n")
    MANIFEST_FILE.write_text(
        f"source: https://github.com/{REPO}\n"
        f"tag: {tag}\n"
        f"commit: {commit}\n"
        f"path: {SOURCE_SUBPATH}\n",
    )
    print(f"wrote {DEST_ROOT} (commit {commit})")


if __name__ == "__main__":
    main()
