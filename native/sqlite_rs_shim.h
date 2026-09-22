#ifndef SQLITE_RS_SHIM_H
#define SQLITE_RS_SHIM_H

#include <Python.h>
#include <sqlite3.h>

size_t sqlite_rs_connection_db_offset(void);
sqlite3 *sqlite_rs_get_connection_db(PyObject *conn);

size_t sqlite_rs_cursor_statement_offset(void);
sqlite3_stmt *sqlite_rs_get_cursor_stmt(PyObject *cursor);
int sqlite_rs_cursor_is_closed(PyObject *cursor);
void sqlite_rs_cursor_release_stmt(PyObject *cursor);

#endif
