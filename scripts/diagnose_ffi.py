#!/usr/bin/env python3
"""Cost of one call into the bundled SQLite, against one staying in-module.

Temporary. The fetch path makes roughly 100M calls into SQLite per 10M rows,
and this crate keeps SQLite as a separate library so a live connection can be
shared with the clone module -- where ADBC statically links its own copy. ADBC
runs at the same speed on Windows as on Linux; this crate is 1.4 to 1.7x
slower there. This measures the boundary as the difference between the two.
"""

import sqlite_rs._core as core

ITERATIONS = 50_000_000

across, inside = core._diagnostic_call_ns(ITERATIONS)  # noqa: SLF001
print(f"  into libsqlite3 : {across:5.2f} ns/call")
print(f"  within _core    : {inside:5.2f} ns/call")
print(f"  boundary        : {across - inside:5.2f} ns/call")
print(f"  over 100M calls : {(across - inside) * 1e8 / 1e9:5.2f} s")
