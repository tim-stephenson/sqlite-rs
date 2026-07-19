/* sqlite_rs_shim.c -- first-party source, NOT derived from CPython source.
 *
 * CPython's stdlib sqlite3 module has no public API for getting at the
 * underlying sqlite3* a Connection wraps, and we don't want to patch
 * CPython's vendored sources (see vendor/cpython/) to add one. Instead,
 * since we vendor the exact connection.h that sqlite_rs's own clone of
 * the _sqlite module is compiled against, this file #includes it
 * unmodified and uses offsetof() to compute the real, compiler-verified
 * byte offset of the `db` field -- rather than hardcoding/guessing a
 * value that would silently go stale on every CPython version bump.
 *
 * This offset is only valid for pysqlite_Connection objects created by
 * sqlite_rs's own compiled clone module. An arbitrary sqlite3.Connection
 * from the system/stdlib sqlite3 module is a different compiled artifact
 * (possibly different compiler, different Py_DEBUG/Py_GIL_DISABLED
 * config) and must never be passed here -- callers are expected to
 * isinstance()-check against the clone module's own Connection type
 * first (see python/sqlite_rs/__init__.py).
 */

#include <stddef.h>

#include "sqlite_rs_shim.h"

/* connection.h declares pysqlite_Connection; pulled in via -I at the
 * vendored CPython _sqlite source directory (see build.rs). */
#include "connection.h"

size_t sqlite_rs_connection_db_offset(void) {
    return offsetof(pysqlite_Connection, db);
}

sqlite3 *sqlite_rs_get_connection_db(PyObject *conn) {
    return *(sqlite3 **)((char *)conn + sqlite_rs_connection_db_offset());
}
