/* sqlite_rs_shim.h -- first-party header, NOT derived from CPython source.
 *
 * Exposes the raw sqlite3* backing a Connection object created by
 * sqlite_rs's own compiled clone of CPython's _sqlite module. See
 * sqlite_rs_shim.c for how the offset is derived and why this is only
 * valid for connections created by that specific clone module.
 */

#ifndef SQLITE_RS_SHIM_H
#define SQLITE_RS_SHIM_H

#include <Python.h>
#include <sqlite3.h>

size_t sqlite_rs_connection_db_offset(void);
sqlite3 *sqlite_rs_get_connection_db(PyObject *conn);

#endif
