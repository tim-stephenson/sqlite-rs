"""Regression test for a specific wheel-packaging failure mode.

Two independently-loaded copies of libsqlite3 in the same process.

sqlite_rs._core (this project's Rust extension) and sqlite_rs.sqlite3._sqlite3
(the CPython clone module, plain C compiled directly by build.rs) are both
meant to dynamically link the exact same bundled libsqlite3 -- see
python/sqlite_rs/__init__.py's module docstring and native/sqlite_rs_shim.c.
query_via_rust/get_raw_db_ptr pass a raw sqlite3* between them, which is only
well-defined if there truly is one shared library instance backing both.

A wheel "repair" step (auditwheel-style manylinux compliance, or an
equivalent macOS/Windows tool) can see _core's dependency on libsqlite3 and
copy/rename it into a private location for _core's own exclusive use --
without touching the clone module (a plain C extension outside that tool's
crate-tracking) or LIBSQLITE3_PATH, both of which still expect the original,
canonical file. Each copy works fine in isolation, so this fails silently
until a raw pointer crosses between them -- observed in practice as a
segmentation fault, not an import error.
"""

import ctypes
import re
import sys
from pathlib import Path
from typing import cast

import sqlite_rs
import sqlite_rs.sqlite3

# This project's own bundled-library naming (build.rs's shared_lib_name():
# libsqlite3.so / libsqlite3.dylib on macOS/Linux, sqlite_rs_libsqlite3.dll on
# Windows -- project-prefixed there specifically to avoid colliding with
# CPython's own bundled sqlite3.dll, see that function's doc comment), plus
# any hash-suffixed rename a repair tool might apply (e.g. delocate's
# libsqlite3-<hash>.dylib). Deliberately does *not* match a versioned system
# SONAME like libsqlite3.so.0 -- a distinct, legitimate library (e.g. behind
# the stdlib sqlite3 module, imported elsewhere in this same test suite) that
# must not be confused with a duplicate of our own.
_BUNDLED_NAME_PATTERN = re.compile(
    r"^(sqlite_rs_lib|lib)?sqlite3(-[0-9a-fA-F]+)?\.(so|dylib|dll)$"
)

# /proc/self/maps lines: address perms offset dev inode [pathname].
_PROC_MAPS_FIELD_COUNT = 6


def _windows_module_paths() -> set[Path]:
    """Every module mapped into this process, via kernel32/psapi.

    Split out of :func:`_loaded_library_paths` only to keep that function
    under ruff's complexity limit.
    """
    # Straight ctypes against kernel32/psapi rather than pywin32, for the
    # same reason the darwin branch above calls dyld directly: it keeps
    # this suite's only third-party requirement pytest. pywin32 has never
    # published a free-threaded wheel for any CPython version, so depending
    # on it here meant Windows could not be tested on 3.13t/3.14t/3.15t at
    # all -- a third-party package gating which interpreters this project
    # can verify itself against.
    #
    # Every attribute looked up on a WinDLL is typed Any -- ctypes resolves
    # foreign functions at runtime and cannot describe their signatures
    # statically -- so each value crossing this boundary is annotated or cast
    # at the point it enters Python, rather than blanket-silencing reportAny.
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    psapi.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD

    list_modules_all = 0x03
    hproc = cast("int", kernel32.GetCurrentProcess())

    # EnumProcessModulesEx reports the bytes it *wanted* via `needed`, so
    # a buffer that was too small is retried at the size it asked for
    # rather than silently truncating the module list.
    needed = wintypes.DWORD()
    capacity = 1024
    while True:
        mods = (wintypes.HMODULE * capacity)()
        if not psapi.EnumProcessModulesEx(
            hproc, mods, ctypes.sizeof(mods), ctypes.byref(needed), list_modules_all
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if needed.value <= ctypes.sizeof(mods):
            break
        capacity = needed.value // ctypes.sizeof(wintypes.HMODULE)

    # MAX_PATH is not a real ceiling for module paths on modern Windows;
    # size for the 32767-wide-char limit instead of truncating long ones.
    buf = ctypes.create_unicode_buffer(32768)
    handles = cast(
        "list[int]", list(mods[: needed.value // ctypes.sizeof(wintypes.HMODULE)])
    )
    paths: set[Path] = set()
    for hmod in handles:
        if psapi.GetModuleFileNameExW(hproc, hmod, buf, len(buf)):
            paths.add(Path(cast("str", buf.value)).resolve())
    return paths


def _loaded_library_paths() -> set[Path]:
    """Every shared library currently mapped into this process, by realpath."""
    if sys.platform == "linux":
        paths: set[Path] = set()
        with Path("/proc/self/maps").open(encoding="utf-8") as maps:
            for line in maps:
                fields = line.split(maxsplit=5)
                if len(fields) == _PROC_MAPS_FIELD_COUNT:
                    path_str = fields[5].strip()
                    if path_str.startswith("/"):
                        paths.add(Path(path_str))
        return {p.resolve() for p in paths}

    if sys.platform == "darwin":
        libc = ctypes.CDLL(None)
        # dyld's own image-enumeration API, not a "private" Python attribute --
        # it's exported by the dynamic linker under this exact C name. ctypes
        # can't statically type a dynamically-resolved C function's signature,
        # hence the Any below regardless of the runtime .restype assignment.
        libc._dyld_get_image_name.restype = ctypes.c_char_p  # noqa: SLF001
        count: int = libc._dyld_image_count()  # noqa: SLF001  # pyright: ignore[reportAny]
        names = (libc._dyld_get_image_name(i) for i in range(count))  # noqa: SLF001
        return {
            Path(name.decode()).resolve()  # pyright: ignore[reportAny]
            for name in names  # pyright: ignore[reportAny]
            if name is not None
        }

    if sys.platform == "win32":
        return _windows_module_paths()

    msg = f"unsupported platform: {sys.platform}"
    raise NotImplementedError(msg)


def test_core_and_clone_module_share_one_bundled_libsqlite3_instance() -> None:
    # Exercise both consumers, in case either's dependency is lazily loaded.
    conn = sqlite_rs.sqlite3.connect(":memory:")
    _ = conn.execute("SELECT 1")
    _ = sqlite_rs.query_via_rust(conn, "SELECT 1")

    bundled = sqlite_rs.LIBSQLITE3_PATH.resolve()
    assert bundled.is_file()

    # Scoped to the sqlite_rs install root (bundled's grandparent, i.e. the
    # site-packages dir containing sqlite_rs/ and any sibling sqlite_rs.libs/
    # sqlite_rs.dylibs/ a repair tool might add) rather than the whole
    # process: the system's own libsqlite3 (e.g. macOS's /usr/lib/
    # libsqlite3.dylib, loaded because the stdlib sqlite3 module -- imported
    # elsewhere in this same test suite -- links it) shares our exact
    # unversioned filename and would otherwise be an indistinguishable,
    # unrelated false positive.
    install_root = bundled.parent.parent
    matches = {
        p
        for p in _loaded_library_paths()
        if _BUNDLED_NAME_PATTERN.match(p.name) and p.is_relative_to(install_root)
    }

    assert matches == {bundled}, (
        f"expected only the bundled {bundled} to be loaded under {install_root}, "
        f"found {matches} -- a second copy usually means a wheel repair step "
        "(auditwheel/delocate) relocated libsqlite3 for one consumer without "
        "updating the others"
    )
