#!/usr/bin/env python3
"""Vendor a pinned SQLite amalgamation into vendor/sqlite/.

Usage:
    python scripts/vendor_sqlite.py [VERSION]

With no argument, re-vendors whatever version is currently pinned in
vendor/sqlite/VERSION. With a version argument (e.g. "3.53.3"), pins and
vendors that version instead, overwriting VERSION.

The download URL and SHA3-256 checksum are read live from sqlite.org's
own machine-readable product data embedded in download.html, rather than
hardcoded here, so bumping the version never requires touching this file.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

DOWNLOAD_PAGE = "https://www.sqlite.org/download.html"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "sqlite"
VERSION_FILE = VENDOR_DIR / "VERSION"
WANTED_FILES = {"sqlite3.c", "sqlite3.h", "sqlite3ext.h"}

PRODUCT_LINE = re.compile(
    r"^PRODUCT,(?P<version>[^,]+),(?P<url>[^,]*sqlite-amalgamation-[^,]+\.zip),"
    r"(?P<size>\d+),(?P<sha3>[0-9a-f]{64})$",
    re.MULTILINE,
)


def find_amalgamation(version: str) -> tuple[str, int, str]:
    """Look up (relative_url, size_bytes, sha3_256_hex) for `version`'s amalgamation zip."""  # noqa: E501
    with urllib.request.urlopen(DOWNLOAD_PAGE) as resp:  # noqa: S310
        page = resp.read().decode("utf-8")

    for match in PRODUCT_LINE.finditer(page):
        if match["version"] == version:
            return match["url"], int(match["size"]), match["sha3"]

    msg = f"could not find amalgamation entry for SQLite {version} on {DOWNLOAD_PAGE}"
    raise SystemExit(msg)


def fetch_and_verify(
    relative_url: str, expected_size: int, expected_sha3: str
) -> bytes:
    """Download the amalgamation zip and verify its size and SHA3-256 before returning it."""  # noqa: E501
    url = f"https://www.sqlite.org/{relative_url}"
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        data = resp.read()

    if len(data) != expected_size:
        msg = f"downloaded {len(data)} bytes, expected {expected_size} from {url}"
        raise SystemExit(msg)

    digest = hashlib.sha3_256(data).hexdigest()
    if digest != expected_sha3:
        msg = f"SHA3-256 mismatch for {url}: got {digest}, expected {expected_sha3}"
        raise SystemExit(msg)

    return data


def unpack(data: bytes) -> None:
    """Extract sqlite3.c/sqlite3.h/sqlite3ext.h from the amalgamation zip into VENDOR_DIR."""  # noqa: E501
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            name = Path(member).name
            if name in WANTED_FILES:
                (VENDOR_DIR / name).write_bytes(zf.read(member))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "version",
        nargs="?",
        help="SQLite version to vendor, e.g. 3.53.3. Defaults to the version in VERSION.",  # noqa: E501
    )
    args = parser.parse_args()

    version = args.version
    if version is None:
        if not VERSION_FILE.exists():
            sys.exit("no version given and vendor/sqlite/VERSION does not exist yet")
        version = VERSION_FILE.read_text().strip()

    relative_url, size, sha3 = find_amalgamation(version)
    print(f"vendoring SQLite {version} from https://www.sqlite.org/{relative_url}")
    data = fetch_and_verify(relative_url, size, sha3)
    unpack(data)
    VERSION_FILE.write_text(version + "\n")
    print(f"wrote {VENDOR_DIR}")


if __name__ == "__main__":
    main()
