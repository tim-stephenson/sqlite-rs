/// The shim in `native/sqlite_rs_shim.c`: it reaches into CPython's private
/// Connection and Cursor structs to pull out the `sqlite3*`/`sqlite3_stmt*`
/// underneath, which is only valid for objects created by this project's own
/// clone of the _sqlite module (`python/sqlite_rs/sqlite3/_sqlite3`).
///
/// Everything else comes from `rusqlite::ffi` (libsqlite3-sys), linked against
/// the bundled `libsqlite_rs_sqlite3` built by build.rs -- see
/// .cargo/config.toml for how that name is forced past libsqlite3-sys's
/// hardcoded `sqlite3`.
mod bind;
mod columns;

mod shim {
    use pyo3::ffi as pyffi;
    use rusqlite::ffi;

    unsafe extern "C" {
        pub fn sqlite_rs_get_connection_db(conn: *mut pyffi::PyObject) -> *mut ffi::sqlite3;
        pub fn sqlite_rs_get_cursor_stmt(cursor: *mut pyffi::PyObject) -> *mut ffi::sqlite3_stmt;
        /// Whether `close()` has been called. A cursor with no statement is
        /// ordinary -- it has no rows left -- so this is what separates that
        /// from one that cannot be read at all.
        pub fn sqlite_rs_cursor_is_closed(cursor: *mut pyffi::PyObject) -> std::os::raw::c_int;
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
    use super::bind;
    use super::columns::{Cell, ColumnBuilder};
    use pyo3::exceptions::{PyTypeError, PyValueError};
    use pyo3::prelude::*;
    use pyo3_arrow::input::AnyRecordBatch;
    use pyo3_arrow::{PyArray, PyTable};
    use arrow_array::RecordBatch;
    use arrow_schema::Schema;
    use rusqlite::ffi;
    use std::os::raw::c_int;
    use std::sync::Arc;

    /// True when this extension was built without optimization, which costs
    /// roughly 3x on a large fetch. Only `scripts/benchmark_fetch_all.py`
    /// reads it, to refuse to report numbers from a debug build.
    #[pymodule_init]
    fn init(module: &Bound<'_, PyModule>) -> PyResult<()> {
        module.add("DEBUG_BUILD", cfg!(debug_assertions))
    }

    /// Run one SQL statement on `connection` and return its columns.
    ///
    /// One Arrow array per result column, each as long as the number of rows;
    /// `[]` for a statement that returns no columns, such as an INSERT.
    ///
    /// `connection` must come from `sqlite_rs.sqlite3.connect()` rather than
    /// the stdlib `sqlite3` module -- see `require_own` for why. Raises
    /// `TypeError` if it does not, and `ValueError` for a closed connection,
    /// a SQL error, or SQL holding more than one statement.
    #[pyfunction]
    fn execute_and_fetch_all(connection: Bound<'_, PyAny>, sql: &str) -> PyResult<Vec<PyArray>> {
        Ok(run_query(connection.py(), connection_db(&connection)?, sql)?.into_arrays())
    }

    /// `execute_and_fetch_all`, as one table rather than a list of columns.
    ///
    /// The same arrays, with their names, behind a single object exporting
    /// `__arrow_c_stream__`. A consumer that reads a whole table -- polars'
    /// `DataFrame`, pyarrow's `table` -- takes it in one call, where a list
    /// of arrays has to be assembled column by column.
    #[pyfunction]
    fn execute_and_fetch_table(connection: Bound<'_, PyAny>, sql: &str) -> PyResult<PyTable> {
        run_query(connection.py(), connection_db(&connection)?, sql)?.into_table()
    }

    /// Run `sql` once per row of `data`, binding each row's values to its
    /// parameters. The Arrow counterpart of `executemany`.
    ///
    /// `data` is anything exporting a table over the Arrow PyCapsule
    /// interface -- a polars `DataFrame`, a pyarrow `Table` or `RecordBatch`,
    /// or what `fetch_table` returns. Returns the total number of rows the
    /// statement changed, which for an `UPDATE` or an upsert is not
    /// necessarily the number of rows given.
    ///
    /// Parameters are matched by position for `?` and by name for `:name`,
    /// whichever the statement uses; see `parameters`. Everything runs inside
    /// one savepoint, so a failure part-way leaves the database as it was,
    /// whether or not a transaction was already open.
    ///
    /// Raises `TypeError` for a connection that is not
    /// `sqlite_rs.sqlite3`'s, or a column whose type has no place in SQLite.
    /// Raises `ValueError` for a closed connection, a SQL error, SQL holding
    /// more than one statement, or columns that do not line up with the
    /// statement's parameters.
    #[pyfunction]
    fn execute_many(connection: Bound<'_, PyAny>, sql: &str, data: AnyRecordBatch) -> PyResult<i64> {
        run_many(connection.py(), connection_db(&connection)?, sql, data)
    }

    /// `execute_many` against a `sqlite3*` the caller already holds.
    #[pyfunction]
    fn execute_many_via_raw_pointer(
        db_ptr: Bound<'_, PyAny>,
        sql: &str,
        data: AnyRecordBatch,
    ) -> PyResult<i64> {
        run_many(db_ptr.py(), raw_pointer(&db_ptr, "db_ptr")? as *mut ffi::sqlite3, sql, data)
    }

    /// Return the `sqlite3*` backing `connection` as a `ctypes.c_void_p`.
    ///
    /// The bundled `libsqlite3` is the one this extension, the clone module
    /// and a `ctypes.CDLL(LIBSQLITE3_PATH)` caller all link, so the pointer
    /// can go straight to any of them and drives the same live connection.
    /// It stays owned by `connection` and is valid until that is closed.
    ///
    /// Same `connection` requirement as `execute_and_fetch_all`.
    #[pyfunction]
    fn get_raw_db_ptr(py: Python<'_>, connection: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        c_void_p(py, connection_db(&connection)? as usize)
    }

    /// `execute_and_fetch_all` against a `sqlite3*` the caller already holds,
    /// from `get_raw_db_ptr` or from an FFI caller's own `sqlite3_open`.
    ///
    /// The mirror of `get_raw_db_ptr`, so a connection can be driven from
    /// either side. There is no object to type-check here: the pointer is
    /// trusted as given. Raises `TypeError` unless it is an address or a
    /// ctypes pointer, and `ValueError` if it is null or the SQL is
    /// rejected.
    #[pyfunction]
    fn execute_and_fetch_all_via_raw_pointer(db_ptr: Bound<'_, PyAny>, sql: &str) -> PyResult<Vec<PyArray>> {
        Ok(run_query(db_ptr.py(), raw_pointer(&db_ptr, "db_ptr")? as *mut ffi::sqlite3, sql)?
            .into_arrays())
    }

    /// `execute_and_fetch_all_via_raw_pointer`, as one table.
    #[pyfunction]
    fn execute_and_fetch_table_via_raw_pointer(db_ptr: Bound<'_, PyAny>, sql: &str) -> PyResult<PyTable> {
        run_query(db_ptr.py(), raw_pointer(&db_ptr, "db_ptr")? as *mut ffi::sqlite3, sql)?.into_table()
    }

    /// Return the `sqlite3_stmt*` backing `cursor` as a `ctypes.c_void_p`,
    /// the cursor-level counterpart to `get_raw_db_ptr`.
    ///
    /// SQLite has no cursor object of its own -- a DB-API cursor is a
    /// prepared statement -- so this is what an FFI caller passes to
    /// `sqlite3_step` and `sqlite3_column_*`. It stays owned by `cursor`.
    ///
    /// `cursor` must come from `sqlite_rs.sqlite3`, for the same reason as
    /// `execute_and_fetch_all`.
    #[pyfunction]
    fn get_raw_stmt_ptr(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        // Unlike a fetch, there is no empty answer to give here: a cursor with
        // no rows left has no statement to point at.
        let stmt = cursor_stmt(py, &cursor)?.ok_or_else(|| {
            PyValueError::new_err("cursor has no rows left, so no sqlite3_stmt* to point at")
        })?;
        c_void_p(py, stmt as usize)
    }

    /// Drain `cursor`'s statement and return its columns, decoded exactly as
    /// `execute_and_fetch_all` decodes them.
    ///
    /// This steps the very statement the Python cursor iterates, so the two
    /// share position: rows returned here are rows the cursor will no longer
    /// yield, and a cursor already exhausted returns none. The cursor is left
    /// exhausted but usable -- `execute()` it again to reuse it.
    ///
    /// Raises `TypeError` for a cursor from the stdlib `sqlite3`, and
    /// `ValueError` for one that is closed or has never executed.
    #[pyfunction]
    fn fetch_all(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<Vec<PyArray>> {
        Ok(drain_cursor(py, &cursor)?.into_arrays())
    }

    /// `fetch_all`, as one table rather than a list of columns.
    #[pyfunction]
    fn fetch_table(py: Python<'_>, cursor: Bound<'_, PyAny>) -> PyResult<PyTable> {
        drain_cursor(py, &cursor)?.into_table()
    }

    /// `fetch_all` against a `sqlite3_stmt*` the caller already holds, from
    /// `get_raw_stmt_ptr` or from an FFI caller's own `sqlite3_prepare_v2`.
    ///
    /// The statement is stepped to exhaustion but never finalized, since it
    /// belongs to the caller. As with the other raw-pointer entry point, the
    /// pointer is trusted as given.
    ///
    /// If the pointer came from a live Python cursor, that cursor must not be
    /// used afterwards. Only a pointer is passed here, so there is no cursor
    /// to put back in order -- and CPython aborts on a statement drained
    /// behind its back (see `fetch_all`). Prefer `fetch_all(cursor)`, which
    /// leaves the cursor correctly exhausted.
    #[pyfunction]
    fn fetch_all_via_raw_pointer(stmt_ptr: Bound<'_, PyAny>) -> PyResult<Vec<PyArray>> {
        Ok(collect_rows(stmt_ptr.py(), raw_pointer(&stmt_ptr, "stmt_ptr")? as *mut ffi::sqlite3_stmt)?
            .into_arrays())
    }

    /// `fetch_all_via_raw_pointer`, as one table.
    #[pyfunction]
    fn fetch_table_via_raw_pointer(stmt_ptr: Bound<'_, PyAny>) -> PyResult<PyTable> {
        collect_rows(stmt_ptr.py(), raw_pointer(&stmt_ptr, "stmt_ptr")? as *mut ffi::sqlite3_stmt)?
            .into_table()
    }

    /// Drain `cursor`'s statement, leaving the cursor fit to be reused.
    fn drain_cursor(py: Python<'_>, cursor: &Bound<'_, PyAny>) -> PyResult<Columns> {
        let Some(stmt) = cursor_stmt(py, cursor)? else {
            return exhausted_columns(cursor);
        };
        let columns = collect_rows(py, stmt);
        // Unconditionally, including on error: a drained statement left in
        // place would abort the interpreter on the cursor's next use.
        unsafe { shim::sqlite_rs_cursor_release_stmt(cursor.as_ptr()) };
        columns
    }

    /// How a statement's parameters line up with the columns given for them.
    enum Parameters {
        /// `?` -- parameter *i* takes column *i*.
        Positional,
        /// `:name` -- parameter *i* takes the column this names.
        Named(Vec<usize>),
    }

    /// Work out which, by asking the statement.
    ///
    /// SQLite reports a name for `:a` and nothing for `?`, so the SQL says
    /// which kind it is and nobody has to pass a flag. A statement mixing the
    /// two is refused rather than guessed at.
    ///
    /// Positional wants exactly as many columns as parameters. Named wants a
    /// column for every parameter and does not mind columns it has no
    /// parameter for -- naming what it wants is the point of naming.
    fn parameters(stmt: *mut ffi::sqlite3_stmt, columns: &[String]) -> PyResult<Parameters> {
        let count = unsafe { ffi::sqlite3_bind_parameter_count(stmt) } as usize;
        let mut order = Vec::with_capacity(count);
        for i in 1..=count {
            let name = unsafe { ffi::sqlite3_bind_parameter_name(stmt, c_int::try_from(i).unwrap_or(0)) };
            if name.is_null() {
                continue;
            }
            let name = unsafe { std::ffi::CStr::from_ptr(name) }.to_string_lossy().into_owned();
            // The leading ':', '@' or '$' is syntax, not part of the name.
            let bare = name.get(1..).unwrap_or_default();
            let Some(column) = columns.iter().position(|c| c == bare) else {
                let given = columns.join(", ");
                return Err(PyValueError::new_err(format!(
                    "parameter {name} has no column to bind: given [{given}]"
                )));
            };
            order.push(column);
        }

        if order.is_empty() {
            if count != columns.len() {
                return Err(PyValueError::new_err(format!(
                    "statement parameters ({count}) and columns ({}) do not match",
                    columns.len()
                )));
            }
            return Ok(Parameters::Positional);
        }
        if order.len() != count {
            return Err(PyValueError::new_err(
                "statement mixes named and positional parameters",
            ));
        }
        Ok(Parameters::Named(order))
    }

    /// Cast every column to the storage class it binds as, once, refusing any
    /// whose type has nowhere to go.
    fn castable(batch: &arrow_array::RecordBatch) -> PyResult<Vec<arrow_array::ArrayRef>> {
        batch
            .schema()
            .fields()
            .iter()
            .zip(batch.columns())
            .map(|(field, column)| {
                let Some(target) = bind::target_type(column.data_type()) else {
                    return Err(PyTypeError::new_err(format!(
                        "column {:?} is {}, which has no SQLite storage class",
                        field.name(),
                        column.data_type()
                    )));
                };
                arrow_cast::cast(column, &target).map_err(|e| {
                    PyValueError::new_err(format!("column {:?}: {e}", field.name()))
                })
            })
            .collect()
    }

    /// A raw SQLite handle carried across a GIL release.
    ///
    /// `*mut` is not `Send`, so a handle cannot enter a `detach` closure on
    /// its own. These belong to the caller's connection or cursor, stay valid
    /// for the length of the call and reach no Python object, so moving one
    /// across is sound. What makes releasing the GIL safe in the first place
    /// is that the bundled SQLite is built multi-thread: a connection is
    /// already documented as not shareable between threads, so there is no
    /// second thread entitled to touch these while they are in use.
    #[derive(Clone, Copy)]
    struct Handle<T>(*mut T);

    // SAFETY: see the type's doc comment.
    unsafe impl<T> Send for Handle<T> {}

    impl<T> Handle<T> {
        /// The pointer back out, inside the closure that released the GIL.
        ///
        /// A method rather than reaching for `.0` directly: under the 2021
        /// closure capture rules a field access captures just that field, so
        /// the closure would try to carry the bare `*mut` -- which is exactly
        /// what is not `Send` -- instead of this wrapper.
        fn get(self) -> *mut T {
            self.0
        }
    }

    /// Run one statement without reading anything back, for savepoints.
    fn exec(db: *mut ffi::sqlite3, sql: &str) -> PyResult<()> {
        let statement = std::ffi::CString::new(sql)
            .map_err(|_| PyValueError::new_err("sql contains a NUL byte"))?;
        let rc = unsafe {
            ffi::sqlite3_exec(db, statement.as_ptr(), None, std::ptr::null_mut(), std::ptr::null_mut())
        };
        if rc == ffi::SQLITE_OK {
            Ok(())
        } else {
            Err(db_err(db, "sqlite3_exec", rc))
        }
    }

    /// Bind one row and run the statement once.
    fn apply_row(
        stmt: *mut ffi::sqlite3_stmt,
        db: *mut ffi::sqlite3,
        columns: &[arrow_array::ArrayRef],
        order: &Parameters,
        row: usize,
        at: usize,
    ) -> PyResult<()> {
        for position in 0..columns.len() {
            let column = match order {
                Parameters::Positional => &columns[position],
                Parameters::Named(order) => match order.get(position) {
                    Some(&index) => &columns[index],
                    // More columns than parameters, which named binding allows.
                    None => continue,
                },
            };
            let parameter = c_int::try_from(position + 1).unwrap_or(0);
            // SQLITE_STATIC: every array outlives the loop that binds from it,
            // so SQLite can borrow rather than copy each value.
            let rc = unsafe {
                match bind::cell_at(column, row) {
                    Cell::Null => ffi::sqlite3_bind_null(stmt, parameter),
                    Cell::Int(v) => ffi::sqlite3_bind_int64(stmt, parameter, v),
                    Cell::Real(v) => ffi::sqlite3_bind_double(stmt, parameter, v),
                    Cell::Text(v) => ffi::sqlite3_bind_text64(
                        stmt,
                        parameter,
                        v.as_ptr().cast(),
                        v.len() as u64,
                        ffi::SQLITE_STATIC(),
                        ffi::SQLITE_UTF8 as u8,
                    ),
                    Cell::Blob(v) => ffi::sqlite3_bind_blob64(
                        stmt,
                        parameter,
                        v.as_ptr().cast(),
                        v.len() as u64,
                        ffi::SQLITE_STATIC(),
                    ),
                }
            };
            if rc != ffi::SQLITE_OK {
                return Err(row_err(db, "sqlite3_bind", rc, at));
            }
        }

        let rc = unsafe { ffi::sqlite3_step(stmt) };
        let stepped = match rc {
            // A statement with a RETURNING clause hands back rows nobody asked
            // for here; stepping to the end is all that is wanted.
            ffi::SQLITE_ROW => {
                while unsafe { ffi::sqlite3_step(stmt) } == ffi::SQLITE_ROW {}
                Ok(())
            }
            ffi::SQLITE_DONE => Ok(()),
            rc => Err(row_err(db, "sqlite3_step", rc, at)),
        };
        unsafe { ffi::sqlite3_reset(stmt) };
        stepped
    }

    /// The body of `execute_many`, wrapped in a savepoint by the caller.
    fn apply_all(
        py: Python<'_>,
        stmt: *mut ffi::sqlite3_stmt,
        db: *mut ffi::sqlite3,
        reader: Box<dyn arrow_array::RecordBatchReader + Send>,
        order: &Parameters,
    ) -> PyResult<i64> {
        let mut changed = 0i64;
        let mut seen = 0usize;
        let handles = (Handle(stmt), Handle(db));
        for batch in reader {
            // Each batch is pulled with the GIL held: the reader is the
            // caller's stream, and a producer is free to implement its
            // capsule by calling back into Python. Only the binding and
            // stepping below is pure SQLite, so only that is detached.
            let batch = batch.map_err(|e| PyValueError::new_err(e.to_string()))?;
            let columns = castable(&batch)?;
            let rows = batch.num_rows();
            changed += py.detach(move || {
                let (stmt, db) = (handles.0.get(), handles.1.get());
                let mut delta = 0i64;
                for row in 0..rows {
                    apply_row(stmt, db, &columns, order, row, seen + row)?;
                    delta += i64::from(unsafe { ffi::sqlite3_changes(db) });
                }
                Ok::<i64, PyErr>(delta)
            })?;
            seen += rows;
        }
        Ok(changed)
    }

    /// Prepare `sql`, then run it once per row of `data`.
    ///
    /// SAVEPOINT rather than BEGIN: it nests inside a transaction the caller
    /// already opened and starts one itself when there is none, so either way
    /// a failure part-way through leaves nothing behind.
    fn run_many(
        py: Python<'_>,
        db: *mut ffi::sqlite3,
        sql: &str,
        data: AnyRecordBatch,
    ) -> PyResult<i64> {
        let reader = data.into_reader()?;
        let columns: Vec<String> = reader
            .schema()
            .fields()
            .iter()
            .map(|field| field.name().clone())
            .collect();

        // Prepared before the savepoint: bad SQL should be an error even when
        // there are no rows to run it against.
        let Some(stmt) = prepare(db, sql)? else {
            return Ok(0);
        };
        // A savepoint only when the caller already has a transaction open,
        // where BEGIN would fail; otherwise a plain transaction, which spares
        // SQLite the bookkeeping that keeping a savepoint rollback-able costs
        // on every row.
        let nested = unsafe { ffi::sqlite3_get_autocommit(db) } == 0;
        let (open, undo, close) = if nested {
            (
                "SAVEPOINT sqlite_rs_execute_many",
                "ROLLBACK TO sqlite_rs_execute_many",
                "RELEASE sqlite_rs_execute_many",
            )
        } else {
            ("BEGIN", "ROLLBACK", "COMMIT")
        };
        let outcome = parameters(stmt, &columns).and_then(|order| {
            exec(db, open)?;
            let applied = apply_all(py, stmt, db, reader, &order);
            if applied.is_err() {
                let _ = exec(db, undo);
                // ROLLBACK TO leaves the savepoint standing, so it still needs
                // popping; a plain ROLLBACK has already ended the transaction.
                if nested {
                    let _ = exec(db, close);
                }
                return applied;
            }
            exec(db, close)?;
            applied
        });
        unsafe { ffi::sqlite3_finalize(stmt) };
        outcome
    }

    /// Prepare `sql` against `db` and decode everything it returns.
    ///
    /// Prepared through the C API rather than `rusqlite::Connection::prepare`
    /// because rusqlite's `Statement` never hands out the `sqlite3_stmt*`
    /// underneath, so its rows could not go through `read_row`. Its own
    /// per-row API costs about 40% more on a large fetch: two mutex-taking
    /// `sqlite3_column_*` calls per cell where `read_row` makes one.
    fn run_query(py: Python<'_>, db: *mut ffi::sqlite3, sql: &str) -> PyResult<Columns> {
        let handle = Handle(db);
        py.detach(move || {
            let db = handle.get();
            let Some(stmt) = prepare(db, sql)? else {
                return Ok(Columns { builders: Vec::new(), names: Vec::new() });
            };
            let rows = scan_columns(stmt);
            unsafe { ffi::sqlite3_finalize(stmt) };
            rows
        })
    }

    /// `None` for SQL that holds no statement at all -- empty, or only a
    /// comment -- which `sqlite3_prepare_v2` reports as success with a null
    /// statement.
    fn prepare(db: *mut ffi::sqlite3, sql: &str) -> PyResult<Option<*mut ffi::sqlite3_stmt>> {
        let len = c_int::try_from(sql.len())
            .map_err(|_| PyValueError::new_err("sql is too long for sqlite3_prepare_v2"))?;
        let mut stmt = std::ptr::null_mut();
        let mut tail = std::ptr::null();
        let rc = unsafe { ffi::sqlite3_prepare_v2(db, sql.as_ptr().cast(), len, &mut stmt, &mut tail) };
        if rc != ffi::SQLITE_OK {
            return Err(db_err(db, "sqlite3_prepare_v2", rc));
        }
        // sqlite3_prepare_v2 compiles the first statement and points `tail` at
        // the rest, which would leave a second statement silently unrun.
        let consumed = unsafe { tail.cast::<u8>().offset_from(sql.as_ptr()) } as usize;
        if !sql[consumed..].trim_matches(|c: char| c.is_whitespace() || c == ';').is_empty() {
            unsafe { ffi::sqlite3_finalize(stmt) };
            return Err(PyValueError::new_err("sql must hold a single statement"));
        }
        Ok((!stmt.is_null()).then_some(stmt))
    }

    /// A finished scan, before it is handed to Python one way or the other.
    struct Columns {
        builders: Vec<ColumnBuilder>,
        names: Vec<String>,
    }

    impl Columns {
        /// One array per column, each carrying its name in its own schema.
        fn into_arrays(self) -> Vec<PyArray> {
            self.finish()
                .map(|(array, field)| PyArray::new(array, Arc::new(field)))
                .collect()
        }

        /// The same arrays behind one `__arrow_c_stream__`. The buffers are
        /// moved, not copied: the table shares them with the arrays above.
        fn into_table(self) -> PyResult<PyTable> {
            let (arrays, fields): (Vec<_>, Vec<_>) = self.finish().unzip();
            let schema = Arc::new(Schema::new(fields));
            if arrays.is_empty() {
                // A statement with no result columns. RecordBatch::try_new
                // cannot represent one: with no array it has no row count.
                return PyTable::try_new(Vec::new(), schema);
            }
            let batch = RecordBatch::try_new(Arc::clone(&schema), arrays)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            PyTable::try_new(vec![batch], schema)
        }

        fn finish(self) -> impl Iterator<Item = (arrow_array::ArrayRef, arrow_schema::Field)> {
            self.builders
                .into_iter()
                .zip(self.names)
                .map(|(builder, name)| builder.finish(&name))
        }
    }

    /// Step an already-prepared statement to exhaustion and decode its rows.
    /// Never finalizes: a cursor's statement belongs to the cursor, and
    /// `run_query` finalizes its own.
    fn collect_rows(py: Python<'_>, stmt: *mut ffi::sqlite3_stmt) -> PyResult<Columns> {
        let handle = Handle(stmt);
        py.detach(move || scan_columns(handle.get()))
    }

    /// `collect_rows` with the GIL already released.
    ///
    /// Touches nothing but SQLite and Arrow builders, so it holds no Python
    /// state; the errors it returns are built lazily and need no interpreter.
    fn scan_columns(stmt: *mut ffi::sqlite3_stmt) -> PyResult<Columns> {
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

        // The values read_row inspects are only safe to touch while the
        // connection's mutex is held; see there. It is recursive, so
        // sqlite3_step re-entering it is fine, and null (a no-op to enter)
        // when the connection was opened SQLITE_OPEN_NOMUTEX.
        let db = unsafe { ffi::sqlite3_db_handle(stmt) };
        let mutex = unsafe { ffi::sqlite3_db_mutex(db) };
        unsafe { ffi::sqlite3_mutex_enter(mutex) };
        let outcome = scan(stmt, db, &mut builders);
        unsafe { ffi::sqlite3_mutex_leave(mutex) };

        outcome.map(|()| Columns { builders, names })
    }

    /// The body of `collect_rows`'s scan, split out so that every way of
    /// leaving it passes through the matching `sqlite3_mutex_leave`.
    fn scan(stmt: *mut ffi::sqlite3_stmt, db: *mut ffi::sqlite3, builders: &mut [ColumnBuilder]) -> PyResult<()> {
        // A DB-API cursor's statement is already positioned on a row, because
        // execute() steps once; a freshly prepared one is not. Stepping first
        // in the former case would silently drop the first row.
        if unsafe { ffi::sqlite3_data_count(stmt) } == 0 && !step(stmt, db)? {
            return Ok(());
        }
        let stride = probe_stride(stmt, builders.len());
        loop {
            read_row(stmt, builders, stride);
            if !step(stmt, db)? {
                return Ok(());
            }
        }
    }

    /// Advance to the next row. `false` once the statement is exhausted.
    fn step(stmt: *mut ffi::sqlite3_stmt, db: *mut ffi::sqlite3) -> PyResult<bool> {
        match unsafe { ffi::sqlite3_step(stmt) } {
            ffi::SQLITE_ROW => Ok(true),
            ffi::SQLITE_DONE => Ok(false),
            rc => Err(db_err(db, "sqlite3_step", rc)),
        }
    }

    /// The distance from one of a row's values to the next, if they really do
    /// sit in an array. Must be called with the statement on a row.
    ///
    /// `sqlite3_column_value` is the only way to reach a value, and it is not
    /// a cheap call: it takes the connection mutex, runs the statement's error
    /// bookkeeping, and only then returns `&stmt->pResultRow[i]`. Making it
    /// per cell rather than per row is about a third of a wide scan.
    ///
    /// That the values are an array is not something the API promises, so this
    /// checks it against the real thing on the first row and gives up if it
    /// does not hold, leaving `read_row` calling per cell. The stride is
    /// `sizeof(Mem)`, a constant of the linked library, so one row settles it
    /// for the whole statement.
    fn probe_stride(stmt: *mut ffi::sqlite3_stmt, ncols: usize) -> Option<usize> {
        let at = |i: usize| unsafe { ffi::sqlite3_column_value(stmt, i as i32) } as usize;
        if ncols < 2 {
            // One call per row either way.
            return None;
        }
        let base = at(0);
        let stride = at(1).checked_sub(base)?;
        if stride == 0 || (2..ncols).any(|i| at(i) != base + stride * i) {
            return None;
        }
        Some(stride)
    }

    /// Decode one row's cells into their columns.
    fn read_row(stmt: *mut ffi::sqlite3_stmt, builders: &mut [ColumnBuilder], stride: Option<usize>) {
        match stride {
            // Skipping `sqlite3_column_value` for the rest of the row also
            // skips the MEM_Static-to-MEM_Ephem flag it flips, which only
            // matters to `sqlite3_value_dup` and `sqlite3_result_value`, and
            // the error bookkeeping, which `sqlite3_step` does anyway.
            Some(stride) => {
                let base = unsafe { ffi::sqlite3_column_value(stmt, 0) }.cast::<u8>();
                for (i, builder) in builders.iter_mut().enumerate() {
                    builder.push(unsafe { decode(base.add(stride * i).cast()) });
                }
            }
            None => {
                for (i, builder) in builders.iter_mut().enumerate() {
                    builder.push(unsafe { decode(ffi::sqlite3_column_value(stmt, i as i32)) });
                }
            }
        }
    }

    /// Read one of a row's values.
    ///
    /// The `sqlite3_value_*` accessors take no mutex, unlike their
    /// `sqlite3_column_*` counterparts, which is what makes one
    /// `sqlite3_column_value` per row worth reaching for. Values obtained that
    /// way are unprotected, so reading them is only safe while the connection
    /// mutex is held -- which `collect_rows` arranges for the whole scan.
    ///
    /// Each accessor is called only for the type the value already has, so
    /// none of them converts it in place.
    unsafe fn decode<'a>(value: *mut ffi::sqlite3_value) -> Cell<'a> {
        unsafe {
            match ffi::sqlite3_value_type(value) {
                ffi::SQLITE_INTEGER => Cell::Int(ffi::sqlite3_value_int64(value)),
                ffi::SQLITE_FLOAT => Cell::Real(ffi::sqlite3_value_double(value)),
                ffi::SQLITE_TEXT => {
                    let bytes = column_slice(ffi::sqlite3_value_text(value), ffi::sqlite3_value_bytes(value));
                    std::str::from_utf8(bytes).map_or(Cell::Blob(bytes), Cell::Text)
                }
                ffi::SQLITE_BLOB => Cell::Blob(column_slice(
                    ffi::sqlite3_value_blob(value).cast::<u8>(),
                    ffi::sqlite3_value_bytes(value),
                )),
                _ => Cell::Null,
            }
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

    /// `db_err`, saying which row of the input was being applied. Built here
    /// rather than by wrapping, so the message does not carry an exception
    /// name in the middle of it.
    fn row_err(db: *mut ffi::sqlite3, call: &str, rc: c_int, row: usize) -> PyErr {
        let msg = unsafe { std::ffi::CStr::from_ptr(ffi::sqlite3_errmsg(db)) };
        PyValueError::new_err(format!(
            "row {row}: {call} failed ({rc}): {}",
            msg.to_string_lossy()
        ))
    }

    fn db_err(db: *mut ffi::sqlite3, call: &str, rc: c_int) -> PyErr {
        let msg = unsafe { std::ffi::CStr::from_ptr(ffi::sqlite3_errmsg(db)) };
        PyValueError::new_err(format!("{call} failed ({rc}): {}", msg.to_string_lossy()))
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

    /// The statement `cursor` is positioned on, or `None` when it has no rows
    /// left.
    ///
    /// CPython releases a cursor's statement as soon as stepping it reaches
    /// SQLITE_DONE, which for a query matching nothing happens inside
    /// `execute()` itself. So no statement is the ordinary state of an
    /// exhausted cursor, not a fault, and `fetchall()` returns `[]` for it.
    fn cursor_stmt(py: Python<'_>, cursor: &Bound<'_, PyAny>) -> PyResult<Option<*mut ffi::sqlite3_stmt>> {
        require_own(py, cursor, "Cursor", "sqlite_rs.sqlite3")?;
        if unsafe { shim::sqlite_rs_cursor_is_closed(cursor.as_ptr()) } != 0 {
            return Err(PyValueError::new_err("cursor is closed"));
        }
        let stmt = unsafe { shim::sqlite_rs_get_cursor_stmt(cursor.as_ptr()) };
        Ok((!stmt.is_null()).then_some(stmt))
    }

    /// The columns of a cursor that has no rows left: none at all for a
    /// statement that returns no columns, and named but empty otherwise.
    ///
    /// The names come from `description`, which the cursor keeps after its
    /// statement is gone. There is nothing left to infer a type from, so each
    /// column is Arrow null -- the same as a query that returns no rows.
    fn exhausted_columns(cursor: &Bound<'_, PyAny>) -> PyResult<Columns> {
        let description = cursor.getattr("description")?;
        let mut names: Vec<String> = Vec::new();
        if !description.is_none() {
            for column in description.try_iter()? {
                names.push(column?.get_item(0)?.extract()?);
            }
        }
        let builders = (0..names.len()).map(|_| ColumnBuilder::default()).collect();
        Ok(Columns { builders, names })
    }
}
