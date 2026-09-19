#include <stddef.h>

#include "sqlite_rs_shim.h"

#include "connection.h" // vendor/cpython/x.y/Modules/_sqlite/connection.h

size_t sqlite_rs_connection_db_offset(void) {
    return offsetof(pysqlite_Connection, db);
}

sqlite3 *sqlite_rs_get_connection_db(PyObject *conn) {
    return *(sqlite3 **)((char *)conn + sqlite_rs_connection_db_offset());
}
