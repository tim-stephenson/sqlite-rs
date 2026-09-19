#ifndef SQLITE_RS_SHIM_H
#define SQLITE_RS_SHIM_H

#include <Python.h>
#include <sqlite3.h>

size_t sqlite_rs_connection_db_offset(void);
sqlite3 *sqlite_rs_get_connection_db(PyObject *conn);

#endif
