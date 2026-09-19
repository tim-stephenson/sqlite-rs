#include <stddef.h>

#include "sqlite_rs_shim.h"

#include "connection.h" // vendor/cpython/x.y/Modules/_sqlite/connection.h
#include "cursor.h"     // also pulls in statement.h

size_t sqlite_rs_connection_db_offset(void) {
    return offsetof(pysqlite_Connection, db);
}

sqlite3 *sqlite_rs_get_connection_db(PyObject *conn) {
    return *(sqlite3 **)((char *)conn + sqlite_rs_connection_db_offset());
}

size_t sqlite_rs_cursor_statement_offset(void) {
    return offsetof(pysqlite_Cursor, statement);
}

// SQLite has no cursor object of its own: a DB-API cursor is backed by a
// prepared statement. pysqlite_Cursor.statement is NULL until execute() runs
// and again once the cursor is closed, so a NULL return is expected, not an
// error.
sqlite3_stmt *sqlite_rs_get_cursor_stmt(PyObject *cursor) {
    pysqlite_Statement *statement =
        *(pysqlite_Statement **)((char *)cursor + sqlite_rs_cursor_statement_offset());
    return statement == NULL ? NULL : statement->st;
}

// Leave `cursor` exactly as CPython leaves it once its own iteration hits
// SQLITE_DONE: statement reset and released. pysqlite_cursor_iternext asserts
// that a non-NULL statement is positioned on a row
// (`assert(sqlite3_data_count(stmt) != 0)`), so draining the statement from
// outside without this would abort the interpreter on the cursor's next use.
void sqlite_rs_cursor_release_stmt(PyObject *cursor) {
    pysqlite_Statement **slot =
        (pysqlite_Statement **)((char *)cursor + sqlite_rs_cursor_statement_offset());
    pysqlite_Statement *statement = *slot;
    if (statement == NULL) {
        return;
    }
    sqlite3_reset(statement->st);
    *slot = NULL;
    Py_DECREF(statement);
}
