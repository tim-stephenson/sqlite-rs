/// Minimal hand-written FFI surface against the dynamically-linked
/// `libsqlite3` built by build.rs (see `vendor/sqlite/`), plus the
/// first-party shim (`native/sqlite_rs_shim.c`) that extracts a raw
/// `sqlite3*` out of a Connection object created by this project's own
/// clone of CPython's sqlite3 module (`python/sqlite_rs/_sqlite3`).
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
    }
}

/// A Python module implemented in Rust.
#[pyo3::pymodule]
mod _core {
    use super::sqlite_ffi::{self, sqlite3_stmt};
    use pyo3::conversion::IntoPyObjectExt;
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;
    use std::ffi::{CStr, CString};

    /// Formats the sum of two numbers as string.
    #[pyfunction]
    fn sum_as_string(a: usize, b: usize) -> PyResult<String> {
        Ok((a + b).to_string())
    }

    /// Run `sql` against the sqlite3* backing `connection`, and return the
    /// result rows. `connection` must be a Connection object created by
    /// sqlite_rs's own clone of CPython's sqlite3 module -- see
    /// `python/sqlite_rs/__init__.py`, which isinstance-checks this before
    /// calling in. This exists to prove that the same live SQLite
    /// connection is genuinely shared between the Python clone module and
    /// this Rust extension (both dynamically link the same `libsqlite3`),
    /// not just built from source-identical but independent copies.
    #[pyfunction]
    fn query_via_shared_connection(
        py: Python<'_>,
        connection: Bound<'_, PyAny>,
        sql: &str,
    ) -> PyResult<Vec<Vec<Py<PyAny>>>> {
        let db = unsafe { sqlite_ffi::sqlite_rs_get_connection_db(connection.as_ptr()) };
        if db.is_null() {
            return Err(PyValueError::new_err(
                "connection has no underlying sqlite3* (is it closed?)",
            ));
        }

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

        let mut rows = Vec::new();
        loop {
            let rc = unsafe { sqlite_ffi::sqlite3_step(stmt) };
            if rc == sqlite_ffi::SQLITE_ROW {
                let n = unsafe { sqlite_ffi::sqlite3_column_count(stmt) };
                let mut row = Vec::with_capacity(n as usize);
                for i in 0..n {
                    row.push(column_value(py, stmt, i)?);
                }
                rows.push(row);
            } else if rc == sqlite_ffi::SQLITE_DONE {
                break;
            } else {
                unsafe { sqlite_ffi::sqlite3_finalize(stmt) };
                return Err(PyValueError::new_err(format!(
                    "sqlite3_step failed ({rc}): {}",
                    sqlite_errmsg(db)
                )));
            }
        }
        unsafe { sqlite_ffi::sqlite3_finalize(stmt) };
        Ok(rows)
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
