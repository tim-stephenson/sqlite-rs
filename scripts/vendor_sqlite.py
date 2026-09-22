#!/usr/bin/env python3
"""Vendor a pinned SQLite amalgamation into vendor/sqlite/.

Usage:
    python scripts/vendor_sqlite.py [VERSION]

With no argument, vendors the newest release sqlite.org offers. With a
version argument (e.g. "3.53.3"), vendors that one instead. Either way the
version vendored is written to vendor/sqlite/VERSION, so re-vendoring the
current pin is `vendor_sqlite.py "$(cat vendor/sqlite/VERSION)"`.

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
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

DOWNLOAD_PAGE = "https://www.sqlite.org/download.html"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "sqlite"
VERSION_FILE = VENDOR_DIR / "VERSION"
WANTED_FILES = {"sqlite3.c", "sqlite3.h", "sqlite3ext.h"}

PRODUCT_LINE = re.compile(
    r"^PRODUCT,(?P<version>[^,]+),(?P<url>[^,]*sqlite-amalgamation-[^,]+\.zip),(?P<size>\d+),(?P<sha3>[0-9a-f]{64})$",
    re.MULTILINE,
)


def find_amalgamations() -> dict[str, tuple[str, int, str]]:
    """Every amalgamation zip download.html offers, by version.

    Each value is (relative_url, size_bytes, sha3_256_hex). The page also
    lists a rolling snapshot, whose `version` is a timestamp rather than a
    release, but it ships no amalgamation zip so PRODUCT_LINE skips it.
    """
    with urllib.request.urlopen(DOWNLOAD_PAGE) as resp:  # pyright: ignore[reportAny]
        page: str = resp.read().decode("utf-8")  # pyright: ignore[reportAny]

    return {
        match["version"]: (match["url"], int(match["size"]), match["sha3"])
        for match in PRODUCT_LINE.finditer(page)
    }


def newest(versions: Iterable[str]) -> str:
    """Return the highest dotted-numeric version, compared a number at a time.

    Not lexicographically: "3.9.0" is older than "3.53.4" but sorts after it.
    """
    numbered = [v for v in versions if re.fullmatch(r"\d+(?:\.\d+)*", v)]
    if not numbered:
        msg = f"no SQLite release found on {DOWNLOAD_PAGE}"
        raise SystemExit(msg)
    return max(numbered, key=lambda v: tuple(int(part) for part in v.split(".")))


def fetch_and_verify(
    relative_url: str, expected_size: int, expected_sha3: str
) -> bytes:
    """Download the amalgamation zip and verify its size and SHA3-256 before returning it."""  # noqa: E501
    url = f"https://www.sqlite.org/{relative_url}"
    with urllib.request.urlopen(url) as resp:  # pyright: ignore[reportAny]
        data: bytes = resp.read()  # pyright: ignore[reportAny]

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
                _ = (VENDOR_DIR / name).write_bytes(zf.read(member))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "version",
        nargs="?",
        help="SQLite version to vendor, e.g. 3.53.3. Defaults to the newest.",
    )
    args = parser.parse_args()

    available = find_amalgamations()
    version = cast("str | None", args.version) or newest(available)
    if version not in available:
        offered = ", ".join(sorted(available)) or "none"
        sys.exit(f"SQLite {version} has no amalgamation there (offered: {offered})")

    relative_url, size, sha3 = available[version]
    print(f"vendoring SQLite {version} from https://www.sqlite.org/{relative_url}")
    data = fetch_and_verify(relative_url, size, sha3)
    unpack(data)
    _ = VERSION_FILE.write_text(version + "\n")
    print(f"wrote {VENDOR_DIR}")


if __name__ == "__main__":
    main()
