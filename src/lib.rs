/// The shim in `native/sqlite_rs_shim.c`: it reaches into CPython's private
/// Connection and Cursor structs to pull out the `sqlite3*`/`sqlite3_stmt*`
/// underneath, which is only valid for objects created by this project's own
/// clone of the _sqlite module (`python/sqlite_rs/sqlite3/_sqlite3`).
///
/// Everything else comes from `rusqlite::ffi` (libsqlite3-sys), linked against
/// the bundled `libsqlite_rs_sqlite3` built by build.rs -- see
/// .cargo/config.toml for how that name is forced past libsqlite3-sys's
/// hardcoded `sqlite3`.
mod columns;

mod shim {
    use pyo3::ffi as pyffi;
    use rusqlite::ffi;

    unsafe extern "C" {
        pub fn sqlite_rs_get_connection_db(conn: *mut pyffi::PyObject) -> *mut ffi::sqlite3;
        pub fn sqlite_rs_get_cursor_stmt(cursor: *mut pyffi::PyObject) -> *mut ffi::sqlite3_stmt;
        /// Reset and release the cursor's statement, restoring the invariant
        /// `pysqlite_cursor_iternext` asserts: a non-NULL statement is
        /// positioned on a row.
        pub fn sqlite_rs_cursor_release_stmt(cursor: *mut pyffi::PyObject);
    }
}

/// A Python module implemented in Rust.
#[pyo3::pymodule]
mod _core {
    use super::shim;
    use super::columns::{Cell, ColumnBuilder};
    use pyo3::exceptions::{PyTypeError, PyValueError};
    use pyo3::prelude::*;
    use pyo3_arrow::PyArray;
    use rusqlite::ffi;
    use rusqlite::types::ValueRef;
    use std::sync::Arc;

    /// True when this extension was built without optimization, which costs
    /// roughly 3x on a large fetch. Only `scripts/benchmark_fetch_all.py`
    /// reads it, to refuse to report numbers from a debug build.
    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        module.add("DEBUG_BUILD", cfg!(debug_assertions))
    }

    /// Run `sql` against the sqlite3* backing `connection`, and return the
    /// result rows. `connection` must be a Connection object created by
    /// sqlite_rs's own clone of CPython's sqlite3 module (see
    /// `require_own_connection` below for why). This exists to prove that
    /// the same live SQLite connection is genuinely shared between the
    /// Python clone module and this Rust extension (both dynamically link
    /// the same `libsqlite3`), not just built from source-identical but
    /// independent copies.
    #[pyfunction]
    fn execute_and_fetch_all(connection: Bound<'_, PyAny>, sql: &str) -> PyResult<Vec<PyArray>> {
        run_query(connection_db(&connection)?, sql)
    }

    /// Return the raw `sqlite3*` backing `connection` as a `ctypes.c_void_p`,
    /// so it can be handed directly to an unrelated FFI caller -- e.g.
    /// ctypes calling straight into the bundled `libsqlite3` -- and used to
    /// operate on the exact same live connection sqlite_rs opened. Same
    /// `connection` requirement as `execute_and_fetch_all` above.
    #[pyfunction]
    fn get_raw_db_ptr(py: Python<'_>, connection: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        c_void_p(py, connection_db(&connection)? as usize)
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
    fn execute_and_fetch_all_via_raw_pointer(db_ptr: Bound<'_, PyAny>, sql: &str) -> PyResult<Vec<PyArray>> {
        run_query(raw_pointer(&db_ptr, "db_ptr")? as *mut ffi::sqlite3, sql)
    }

    /// Return the raw `sqlite3_stmt*` backing `cursor` as a
    /// `ctypes.c_void_p`, the cursor-level counterpart to
    /// `get_raw_db_ptr`. SQLite has no cursor object of its own -- a DB-API
    /// cursor is a prepared statement -- so this is a `sqlite3_stmt*`, which
    /// is what an FFI caller passes to `sqlite3_step`, `sqlite3_column_*`
    /// and friends. `cursor` must come from `sqlite_rs.sqlite3`, for the
    /// same struct-layout reason as `execute_and_fetch_all`.
    #[pyfunction]
    fn get_raw_stmt_ptr(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        c_void_p(py, cursor_stmt(py, &cursor)? as usize)
    }

    /// Drain `cursor`'s underlying statement from the Rust side and return
    /// the rows, decoding columns exactly as `execute_and_fetch_all` does.
    ///
    /// This steps the very same `sqlite3_stmt*` the Python cursor iterates,
    /// so the two share position: rows returned here are rows the cursor
    /// will no longer yield, and a cursor already exhausted returns none.
    /// That shared state is the point -- it is the cursor-level equivalent
    /// of two consumers sharing one live connection.
    #[pyfunction]
    fn fetch_all(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<Vec<PyArray>> {
        let stmt = cursor_stmt(py, &cursor)?;
        let rows = collect_rows(stmt);
        // Unconditionally, including on error: a drained statement left in
        // place would abort the interpreter on the cursor's next use.
        unsafe { shim::sqlite_rs_cursor_release_stmt(cursor.as_ptr()) };
        rows
    }

    /// `fetch_all` against a raw `sqlite3_stmt*`, as returned by
    /// `get_raw_stmt_ptr` or by an unrelated FFI caller's own
    /// `sqlite3_prepare_v2`. The mirror of `execute_and_fetch_all_via_raw_pointer`: an
    /// arbitrary pointer is trusted as-is, per its documented contract.
    ///
    /// If the pointer came from a live Python cursor, that cursor must not be
    /// used afterwards. Only a pointer is passed here, so there is no cursor
    /// to put back in order -- and CPython aborts on a statement drained
    /// behind its back (see `fetch_all`). Prefer `fetch_all(cursor)`, which
    /// leaves the cursor correctly exhausted.
    #[pyfunction]
    fn fetch_all_via_raw_pointer(stmt_ptr: Bound<'_, PyAny>) -> PyResult<Vec<PyArray>> {
        collect_rows(raw_pointer(&stmt_ptr, "stmt_ptr")? as *mut ffi::sqlite3_stmt)
    }

    /// Borrow `db` as a rusqlite `Connection` and run `sql` through it.
    ///
    /// `from_handle` borrows rather than owns: the returned Connection does
    /// not close the database when dropped, which is essential here because
    /// the handle belongs to the Python Connection object.
    fn run_query(db: *mut ffi::sqlite3, sql: &str) -> PyResult<Vec<PyArray>> {
        let conn = unsafe { rusqlite::Connection::from_handle(db) }.map_err(sqlite_err)?;
        let mut stmt = conn.prepare(sql).map_err(sqlite_err)?;
        let names: Vec<String> = stmt.column_names().into_iter().map(str::to_owned).collect();
        let mut builders: Vec<ColumnBuilder> = (0..names.len()).map(|_| ColumnBuilder::default()).collect();

        let mut rows = stmt.query([]).map_err(sqlite_err)?;
        while let Some(row) = rows.next().map_err(sqlite_err)? {
            for (i, builder) in builders.iter_mut().enumerate() {
                builder.push(cell_of(row.get_ref(i).map_err(sqlite_err)?));
            }
        }
        Ok(finish(builders, &names))
    }

    fn cell_of(value: ValueRef<'_>) -> Cell<'_> {
        match value {
            ValueRef::Null => Cell::Null,
            ValueRef::Integer(v) => Cell::Int(v),
            ValueRef::Real(v) => Cell::Real(v),
            // SQLite does not guarantee TEXT is valid UTF-8; the lossy path
            // below is only reachable for deliberately malformed data.
            ValueRef::Text(v) => std::str::from_utf8(v).map_or(Cell::Blob(v), Cell::Text),
            ValueRef::Blob(v) => Cell::Blob(v),
        }
    }

    fn finish(builders: Vec<ColumnBuilder>, names: &[String]) -> Vec<PyArray> {
        builders
            .into_iter()
            .zip(names)
            .map(|(builder, name)| {
                let (array, field) = builder.finish(name);
                PyArray::new(array, Arc::new(field))
            })
            .collect()
    }

    /// Step an already-prepared statement to exhaustion and decode its rows.
    /// Never finalizes: `run_query`'s statements are owned by rusqlite, while
    /// a cursor's statement belongs to the cursor.
    ///
    /// Raw `ffi` rather than rusqlite, because rusqlite offers no way to
    /// borrow an existing `sqlite3_stmt*` the way `Connection::from_handle`
    /// borrows a `sqlite3*`.
    fn collect_rows(stmt: *mut ffi::sqlite3_stmt) -> PyResult<Vec<PyArray>> {
        let ncols = unsafe { ffi::sqlite3_column_count(stmt) };
        let names: Vec<String> = (0..ncols)
            .map(|i| unsafe {
                let ptr = ffi::sqlite3_column_name(stmt, i);
                if ptr.is_null() {
                    format!("column{i}")
                } else {
                    std::ffi::CStr::from_ptr(ptr).to_string_lossy().into_owned()
                }
            })
            .collect();
        let mut builders: Vec<ColumnBuilder> = (0..ncols).map(|_| ColumnBuilder::default()).collect();

        // A DB-API cursor's statement is already positioned on a row, because
        // execute() steps once; a freshly prepared one is not. Stepping first
        // in the former case would silently drop the first row.
        if unsafe { ffi::sqlite3_data_count(stmt) } > 0 {
            read_row(stmt, &mut builders);
        }
        loop {
            match unsafe { ffi::sqlite3_step(stmt) } {
                ffi::SQLITE_ROW => read_row(stmt, &mut builders),
                ffi::SQLITE_DONE => return Ok(finish(builders, &names)),
                rc => {
                    let db = unsafe { ffi::sqlite3_db_handle(stmt) };
                    let msg = unsafe { std::ffi::CStr::from_ptr(ffi::sqlite3_errmsg(db)) };
                    return Err(PyValueError::new_err(format!(
                        "sqlite3_step failed ({rc}): {}",
                        msg.to_string_lossy()
                    )));
                }
            }
        }
    }

    fn read_row(stmt: *mut ffi::sqlite3_stmt, builders: &mut [ColumnBuilder]) {
        for (i, builder) in builders.iter_mut().enumerate() {
            let i = i as i32;
            let cell = unsafe {
                match ffi::sqlite3_column_type(stmt, i) {
                    ffi::SQLITE_INTEGER => Cell::Int(ffi::sqlite3_column_int64(stmt, i)),
                    ffi::SQLITE_FLOAT => Cell::Real(ffi::sqlite3_column_double(stmt, i)),
                    ffi::SQLITE_TEXT => {
                        let bytes = column_slice(ffi::sqlite3_column_text(stmt, i), ffi::sqlite3_column_bytes(stmt, i));
                        std::str::from_utf8(bytes).map_or(Cell::Blob(bytes), Cell::Text)
                    }
                    ffi::SQLITE_BLOB => Cell::Blob(column_slice(
                        ffi::sqlite3_column_blob(stmt, i).cast::<u8>(),
                        ffi::sqlite3_column_bytes(stmt, i),
                    )),
                    _ => Cell::Null,
                }
            };
            builder.push(cell);
        }
    }

    /// SQLite returns a null pointer for a zero-length text/blob, which
    /// `from_raw_parts` will not accept.
    unsafe fn column_slice<'a>(ptr: *const u8, len: std::os::raw::c_int) -> &'a [u8] {
        if ptr.is_null() || len <= 0 {
            &[]
        } else {
            unsafe { std::slice::from_raw_parts(ptr, len as usize) }
        }
    }

    fn sqlite_err(e: rusqlite::Error) -> PyErr {
        PyValueError::new_err(e.to_string())
    }

    fn c_void_p(py: Python<'_>, addr: usize) -> PyResult<Py<PyAny>> {
        Ok(py.import("ctypes")?.getattr("c_void_p")?.call1((addr,))?.unbind())
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
    /// stdlib `sqlite3.Connection`: the shim extracts the raw `sqlite3*` by
    /// reading CPython's private Connection struct layout, which is only
    /// guaranteed to match for connections created by this package's own
    /// compiled clone module.
    ///
    /// `sqlite_rs.sqlite3` is looked up by name rather than imported at
    /// module init time, since by the time any `#[pyfunction]` here is
    /// actually called, `sqlite_rs` (and therefore `sqlite_rs.sqlite3`) is
    /// necessarily already fully imported -- this module IS a submodule of
    /// it.
    fn require_own(py: Python<'_>, obj: &Bound<'_, PyAny>, class: &str, source: &str) -> PyResult<()> {
        let expected = py.import("sqlite_rs.sqlite3")?.getattr(class)?;
        if obj.is_instance(&expected)? {
            Ok(())
        } else {
            Err(PyTypeError::new_err(format!(
                "{} must be a {class} from {source} (sqlite_rs's own sqlite3 clone), \
                 not the stdlib sqlite3 module",
                class.to_lowercase()
            )))
        }
    }

    fn connection_db(connection: &Bound<'_, PyAny>) -> PyResult<*mut ffi::sqlite3> {
        let py = connection.py();
        require_own(py, connection, "Connection", "sqlite_rs.sqlite3.connect()")?;
        let db = unsafe { shim::sqlite_rs_get_connection_db(connection.as_ptr()) };
        if db.is_null() {
            return Err(PyValueError::new_err(
                "connection has no underlying sqlite3* (is it closed?)",
            ));
        }
        Ok(db)
    }

    fn cursor_stmt(py: Python<'_>, cursor: &Bound<'_, PyAny>) -> PyResult<*mut ffi::sqlite3_stmt> {
        require_own(py, cursor, "Cursor", "sqlite_rs.sqlite3")?;
        let stmt = unsafe { shim::sqlite_rs_get_cursor_stmt(cursor.as_ptr()) };
        if stmt.is_null() {
            return Err(PyValueError::new_err(
                "cursor has no underlying sqlite3_stmt* (has it executed a statement, and is \
                 it still open?)",
            ));
        }
        Ok(stmt)
    }
}
