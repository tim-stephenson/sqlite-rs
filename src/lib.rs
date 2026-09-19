/// Minimal hand-written FFI surface against the dynamically-linked
/// `libsqlite3` built by build.rs (see `vendor/sqlite/`), plus the
/// first-party shim (`native/sqlite_rs_shim.c`) that extracts a raw
/// `sqlite3*` out of a Connection object created by this project's own
/// clone of CPython's sqlite3 module (`python/sqlite_rs/sqlite3/_sqlite3`).
mod sqlite_ffi {
    use pyo3::ffi as pyffi;
    use std::os::raw::{c_char, c_int, c_uchar};

    #[allow(non_camel_case_types)]
    pub enum sqlite3 {}
    #[allow(non_camel_case_types)]
    pub enum sqlite3_stmt {}

    pub const SQLITE_ROW: c_int = 100;
    pub const SQLITE_DONE: c_int = 101;
    pub const SQLITE_INTEGER: c_int = 1;
    pub const SQLITE_FLOAT: c_int = 2;
    pub const SQLITE_TEXT: c_int = 3;

    unsafe extern "C" {
        /// native/sqlite_rs_shim.c -- only valid for Connection objects
        /// created by sqlite_rs's own clone of the _sqlite module.
        pub fn sqlite_rs_get_connection_db(conn: *mut pyffi::PyObject) -> *mut sqlite3;

        /// native/sqlite_rs_shim.c -- the `sqlite3_stmt*` backing a DB-API
        /// cursor, or NULL before execute() or after close().
        pub fn sqlite_rs_get_cursor_stmt(cursor: *mut pyffi::PyObject) -> *mut sqlite3_stmt;

        /// native/sqlite_rs_shim.c -- reset and release the cursor's
        /// statement, restoring the invariant CPython's iternext asserts.
        pub fn sqlite_rs_cursor_release_stmt(cursor: *mut pyffi::PyObject);

        pub fn sqlite3_prepare_v2(
            db: *mut sqlite3,
            sql: *const c_char,
            n_byte: c_int,
            stmt: *mut *mut sqlite3_stmt,
            tail: *mut *const c_char,
        ) -> c_int;
        pub fn sqlite3_step(stmt: *mut sqlite3_stmt) -> c_int;
        pub fn sqlite3_finalize(stmt: *mut sqlite3_stmt) -> c_int;
        pub fn sqlite3_column_count(stmt: *mut sqlite3_stmt) -> c_int;
        pub fn sqlite3_column_type(stmt: *mut sqlite3_stmt, i: c_int) -> c_int;
        pub fn sqlite3_column_int64(stmt: *mut sqlite3_stmt, i: c_int) -> i64;
        pub fn sqlite3_column_double(stmt: *mut sqlite3_stmt, i: c_int) -> f64;
        pub fn sqlite3_column_text(stmt: *mut sqlite3_stmt, i: c_int) -> *const c_uchar;
        pub fn sqlite3_column_bytes(stmt: *mut sqlite3_stmt, i: c_int) -> c_int;
        pub fn sqlite3_errmsg(db: *mut sqlite3) -> *const c_char;
        pub fn sqlite3_db_handle(stmt: *mut sqlite3_stmt) -> *mut sqlite3;
        pub fn sqlite3_data_count(stmt: *mut sqlite3_stmt) -> c_int;
    }
}

/// A Python module implemented in Rust.
#[pyo3::pymodule]
mod _core {
    use super::sqlite_ffi::{self, sqlite3_stmt};
    use pyo3::conversion::IntoPyObjectExt;
    use pyo3::exceptions::{PyTypeError, PyValueError};
    use pyo3::prelude::*;
    use std::ffi::{CStr, CString};

    /// Run `sql` against the sqlite3* backing `connection`, and return the
    /// result rows. `connection` must be a Connection object created by
    /// sqlite_rs's own clone of CPython's sqlite3 module (see
    /// `require_own_connection` below for why). This exists to prove that
    /// the same live SQLite connection is genuinely shared between the
    /// Python clone module and this Rust extension (both dynamically link
    /// the same `libsqlite3`), not just built from source-identical but
    /// independent copies.
    #[pyfunction]
    fn query_via_rust(py: Python<'_>, connection: Bound<'_, PyAny>, sql: &str) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        run_query(py, connection_db(py, connection)?, sql)
    }

    /// Return the raw `sqlite3*` backing `connection` as a `ctypes.c_void_p`,
    /// so it can be handed directly to an unrelated FFI caller -- e.g.
    /// ctypes calling straight into the bundled `libsqlite3` -- and used to
    /// operate on the exact same live connection sqlite_rs opened. Same
    /// `connection` requirement as `query_via_rust` above.
    #[pyfunction]
    fn get_raw_db_ptr(py: Python<'_>, connection: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let addr = connection_db(py, connection)? as usize;
        let c_void_p = py.import("ctypes")?.getattr("c_void_p")?;
        Ok(c_void_p.call1((addr,))?.unbind())
    }

    /// Run `sql` against the raw `sqlite3*` at `db_ptr` (as returned by, for
    /// instance, ctypes calling `sqlite3_open` against the bundled
    /// `libsqlite3` directly). This is the mirror image of
    /// `get_raw_db_ptr`: it lets an external caller's connection be driven
    /// from the Rust side, proving the sharing works in both directions,
    /// not just from a `Connection` object outward. Unlike the two
    /// functions above, there's no type to check here -- an arbitrary raw
    /// pointer is trusted as-is, per its documented contract.
    #[pyfunction]
    fn query_via_raw_pointer(py: Python<'_>, db_ptr: Bound<'_, PyAny>, sql: &str) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        let db = raw_pointer(&db_ptr, "db_ptr")? as *mut sqlite_ffi::sqlite3;
        run_query(py, db, sql)
    }

    /// Return the raw `sqlite3_stmt*` backing `cursor` as a
    /// `ctypes.c_void_p`, the cursor-level counterpart to
    /// `get_raw_db_ptr`. SQLite has no cursor object of its own -- a DB-API
    /// cursor is a prepared statement -- so this is a `sqlite3_stmt*`, which
    /// is what an FFI caller passes to `sqlite3_step`, `sqlite3_column_*`
    /// and friends. `cursor` must come from `sqlite_rs.sqlite3`, for the
    /// same struct-layout reason as `query_via_rust`.
    #[pyfunction]
    fn get_raw_stmt_ptr(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let addr = cursor_stmt(py, &cursor)? as usize;
        let c_void_p = py.import("ctypes")?.getattr("c_void_p")?;
        Ok(c_void_p.call1((addr,))?.unbind())
    }

    /// Drain `cursor`'s underlying statement from the Rust side and return
    /// the rows, decoding columns exactly as `query_via_rust` does.
    ///
    /// This steps the very same `sqlite3_stmt*` the Python cursor iterates,
    /// so the two share position: rows returned here are rows the cursor
    /// will no longer yield, and a cursor already exhausted returns none.
    /// That shared state is the point -- it is the cursor-level equivalent
    /// of two consumers sharing one live connection.
    #[pyfunction]
    fn fetch_all(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        let stmt = cursor_stmt(py, &cursor)?;
        let rows = collect_rows(py, stmt);
        // Unconditionally, including on error: a drained statement left in
        // place would abort the interpreter on the cursor's next use.
        unsafe { sqlite_ffi::sqlite_rs_cursor_release_stmt(cursor.as_ptr()) };
        rows
    }

    /// `fetch_all` against a raw `sqlite3_stmt*`, as returned by
    /// `get_raw_stmt_ptr` or by an unrelated FFI caller's own
    /// `sqlite3_prepare_v2`. The mirror of `query_via_raw_pointer`: an
    /// arbitrary pointer is trusted as-is, per its documented contract.
    ///
    /// If the pointer came from a live Python cursor, that cursor must not be
    /// used afterwards. Only a pointer is passed here, so there is no cursor
    /// to put back in order -- and CPython aborts on a statement drained
    /// behind its back (see `fetch_all`). Prefer `fetch_all(cursor)`, which
    /// leaves the cursor correctly exhausted.
    #[pyfunction]
    fn fetch_all_via_raw_pointer(py: Python<'_>, stmt_ptr: Bound<'_, PyAny>) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        collect_rows(py, raw_pointer(&stmt_ptr, "stmt_ptr")? as *mut sqlite3_stmt)
    }

    /// Accept either a plain address or the `ctypes.c_void_p` that
    /// `get_raw_db_ptr`/`get_raw_stmt_ptr` hand back, so a pointer can be
    /// passed straight back in without unwrapping it. ctypes scalars keep
    /// their address in `.value`, which is `None` for NULL; anything else is
    /// read as an integer.
    fn raw_pointer(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<usize> {
        let value = obj.getattr("value").unwrap_or_else(|_| obj.clone());
        if value.is_none() {
            return Err(PyValueError::new_err(format!("{name} is null")));
        }
        let addr: usize = value.extract().map_err(|_| {
            PyTypeError::new_err(format!(
                "{name} must be an int address or a ctypes pointer, not {}",
                obj.get_type().name().map_or_else(|_| "?".to_string(), |n| n.to_string())
            ))
        })?;
        if addr == 0 {
            return Err(PyValueError::new_err(format!("{name} is null")));
        }
        Ok(addr)
    }

    /// `connection` must be an instance of `sqlite_rs.sqlite3.Connection`
    /// (this project's own clone of CPython's sqlite3 module), not the
    /// stdlib `sqlite3.Connection`: the shim below extracts the raw
    /// `sqlite3*` by reading CPython's private Connection struct layout,
    /// which is only guaranteed to match for connections created by this
    /// package's own compiled clone module.
    ///
    /// `sqlite_rs.sqlite3` is looked up by name rather than imported at
    /// module init time, since by the time any `#[pyfunction]` here is
    /// actually called, `sqlite_rs` (and therefore `sqlite_rs.sqlite3`) is
    /// necessarily already fully imported -- this module IS a submodule of
    /// it.
    fn require_own_connection(py: Python<'_>, connection: &Bound<'_, PyAny>) -> PyResult<()> {
        let connection_type = py.import("sqlite_rs.sqlite3")?.getattr("Connection")?;
        if connection.is_instance(&connection_type)? {
            Ok(())
        } else {
            Err(PyTypeError::new_err(
                "connection must be a Connection from sqlite_rs.sqlite3.connect() (sqlite_rs's \
                 own sqlite3 clone), not the stdlib sqlite3 module",
            ))
        }
    }

    fn connection_db(py: Python<'_>, connection: Bound<'_, PyAny>) -> PyResult<*mut sqlite_ffi::sqlite3> {
        require_own_connection(py, &connection)?;
        let db = unsafe { sqlite_ffi::sqlite_rs_get_connection_db(connection.as_ptr()) };
        if db.is_null() {
            return Err(PyValueError::new_err(
                "connection has no underlying sqlite3* (is it closed?)",
            ));
        }
        Ok(db)
    }

    /// Same struct-layout requirement as `connection_db`: the shim reads
    /// CPython's private Cursor layout, which only matches for cursors from
    /// this package's own compiled clone module.
    fn cursor_stmt(py: Python<'_>, cursor: &Bound<'_, PyAny>) -> PyResult<*mut sqlite3_stmt> {
        let cursor_type = py.import("sqlite_rs.sqlite3")?.getattr("Cursor")?;
        if !cursor.is_instance(&cursor_type)? {
            return Err(PyTypeError::new_err(
                "cursor must be a Cursor from sqlite_rs.sqlite3 (sqlite_rs's own sqlite3 \
                 clone), not the stdlib sqlite3 module",
            ));
        }
        let stmt = unsafe { sqlite_ffi::sqlite_rs_get_cursor_stmt(cursor.as_ptr()) };
        if stmt.is_null() {
            return Err(PyValueError::new_err(
                "cursor has no underlying sqlite3_stmt* (has it executed a statement, and is \
                 it still open?)",
            ));
        }
        Ok(stmt)
    }

    fn run_query(py: Python<'_>, db: *mut sqlite_ffi::sqlite3, sql: &str) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        let c_sql = CString::new(sql).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut stmt: *mut sqlite3_stmt = std::ptr::null_mut();
        let rc = unsafe {
            sqlite_ffi::sqlite3_prepare_v2(db, c_sql.as_ptr(), -1, &mut stmt, std::ptr::null_mut())
        };
        if rc != 0 {
            return Err(PyValueError::new_err(format!(
                "sqlite3_prepare_v2 failed ({rc}): {}",
                sqlite_errmsg(db)
            )));
        }

        let rows = collect_rows(py, stmt);
        unsafe { sqlite_ffi::sqlite3_finalize(stmt) };
        rows
    }

    /// Step an already-prepared statement to exhaustion and decode its rows.
    /// Never finalizes: `run_query` owns the statements it prepares, while a
    /// cursor's statement belongs to the cursor.
    fn collect_rows(py: Python<'_>, stmt: *mut sqlite3_stmt) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        let mut rows = Vec::new();
        // A DB-API cursor's statement is already positioned on a row, because
        // execute() steps once; a freshly prepared one is not. Stepping first
        // in the former case would silently drop the first row.
        if unsafe { sqlite_ffi::sqlite3_data_count(stmt) } > 0 {
            rows.push(read_row(py, stmt)?);
        }
        loop {
            let rc = unsafe { sqlite_ffi::sqlite3_step(stmt) };
            if rc == sqlite_ffi::SQLITE_ROW {
                rows.push(read_row(py, stmt)?);
            } else if rc == sqlite_ffi::SQLITE_DONE {
                return Ok(rows);
            } else {
                let db = unsafe { sqlite_ffi::sqlite3_db_handle(stmt) };
                return Err(PyValueError::new_err(format!(
                    "sqlite3_step failed ({rc}): {}",
                    sqlite_errmsg(db)
                )));
            }
        }
    }

    fn read_row(py: Python<'_>, stmt: *mut sqlite3_stmt) -> PyResult<Vec<Py<PyAny>>> {
        let n = unsafe { sqlite_ffi::sqlite3_column_count(stmt) };
        let mut row = Vec::with_capacity(n as usize);
        for i in 0..n {
            row.push(column_value(py, stmt, i)?);
        }
        Ok(row)
    }

    fn sqlite_errmsg(db: *mut sqlite_ffi::sqlite3) -> String {
        unsafe { CStr::from_ptr(sqlite_ffi::sqlite3_errmsg(db)) }
            .to_string_lossy()
            .into_owned()
    }

    fn column_value(py: Python<'_>, stmt: *mut sqlite3_stmt, i: i32) -> PyResult<Py<PyAny>> {
        unsafe {
            match sqlite_ffi::sqlite3_column_type(stmt, i) {
                sqlite_ffi::SQLITE_INTEGER => sqlite_ffi::sqlite3_column_int64(stmt, i).into_py_any(py),
                sqlite_ffi::SQLITE_FLOAT => sqlite_ffi::sqlite3_column_double(stmt, i).into_py_any(py),
                sqlite_ffi::SQLITE_TEXT => {
                    let ptr = sqlite_ffi::sqlite3_column_text(stmt, i);
                    let len = sqlite_ffi::sqlite3_column_bytes(stmt, i) as usize;
                    let bytes = std::slice::from_raw_parts(ptr, len);
                    String::from_utf8_lossy(bytes).into_owned().into_py_any(py)
                }
                _ => Ok(py.None()),
            }
        }
    }
}
