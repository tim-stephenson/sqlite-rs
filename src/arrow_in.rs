//! Arrow tables on their way in, over the C stream interface.
//!
//! The same import `pyo3_arrow` would do, with one repair. A null column has
//! no physical storage, so the C data interface gives it no buffers at all:
//! `n_buffers` must be 0. polars exports 1 (a validity buffer it never fills,
//! left as a null pointer), and arrow-rs believes the count, asks how wide
//! buffer 0 of a null array is, and fails:
//!
//! ```text
//! The datatype "Null" doesn't expect buffer at index 0.
//! ```
//!
//! pyarrow exports 0 for the same column and is happy to import polars', so
//! the disagreement costs nothing but a correct count. Rather than make a
//! `DataFrame` with an all-null column unusable -- which is an ordinary thing
//! to have, and gives no hint that the dtype is at fault -- the count is put
//! back to 0 on the way past. Nothing is read through it either way: the
//! pointer polars leaves there is null, and a repaired array reads no buffers.

use std::ffi::CStr;
use std::sync::Arc;

use arrow_array::RecordBatch;
use arrow_array::RecordBatchOptions;
use arrow_array::RecordBatchReader;
use arrow_array::StructArray;
use arrow_array::ffi::from_ffi_and_data_type;
use arrow_array::ffi_stream::FFI_ArrowArrayStream;
use arrow_data::ffi::FFI_ArrowArray;
use arrow_schema::ffi::FFI_ArrowSchema;
use arrow_schema::{ArrowError, DataType, Schema, SchemaRef};

/// A reader over a C stream that repairs null columns as they arrive.
pub struct RepairingReader {
    stream: FFI_ArrowArrayStream,
    schema: SchemaRef,
    /// Top-level column positions whose type is null, so only those are
    /// looked at per batch rather than every child of every batch.
    nulls: Vec<usize>,
}

// SAFETY: the stream is owned outright -- moved out of the producer's capsule,
// which is left empty -- and is only ever touched through `&mut self`.
unsafe impl Send for RepairingReader {}

impl RepairingReader {
    /// Take ownership of `stream` and read its schema.
    pub fn try_new(mut stream: FFI_ArrowArrayStream) -> Result<Self, ArrowError> {
        if stream.release.is_none() {
            return Err(ArrowError::CDataInterface(
                "input stream is already released".to_string(),
            ));
        }
        let mut raw = FFI_ArrowSchema::empty();
        let code = unsafe {
            let get_schema = stream.get_schema.ok_or_else(|| {
                ArrowError::CDataInterface("input stream has no get_schema".to_string())
            })?;
            get_schema(&mut stream, &mut raw)
        };
        if code != 0 {
            return Err(ArrowError::CDataInterface(format!(
                "cannot get schema from input stream (error code {code})"
            )));
        }
        let schema = Schema::try_from(&raw)?;
        let nulls = schema
            .fields()
            .iter()
            .enumerate()
            .filter(|(_, field)| *field.data_type() == DataType::Null)
            .map(|(i, _)| i)
            .collect();
        Ok(Self { stream, schema: Arc::new(schema), nulls })
    }

    /// Put `n_buffers` back to 0 on this batch's null columns.
    ///
    /// Only the columns the schema already called null, and only their buffer
    /// count: the array is otherwise handed on exactly as it arrived, still
    /// carrying the producer's own release callback.
    fn repair(&self, array: &mut FFI_ArrowArray) {
        for &i in &self.nulls {
            if i as i64 >= array.n_children || array.children.is_null() {
                continue;
            }
            // SAFETY: `i` is below n_children, and the producer guarantees
            // `children` points at that many valid FFI_ArrowArray pointers.
            unsafe {
                let child = *array.children.add(i);
                if !child.is_null() {
                    (*child).n_buffers = 0;
                }
            }
        }
    }

    fn last_error(&mut self) -> Option<String> {
        let get_last_error = self.stream.get_last_error?;
        let text = unsafe { get_last_error(&mut self.stream) };
        if text.is_null() {
            return None;
        }
        Some(unsafe { CStr::from_ptr(text) }.to_string_lossy().into_owned())
    }
}

impl Iterator for RepairingReader {
    type Item = Result<RecordBatch, ArrowError>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut array = FFI_ArrowArray::empty();
        let get_next = self.stream.get_next?;
        let code = unsafe { get_next(&mut self.stream, &mut array) };
        if code != 0 {
            let reported = self.last_error().unwrap_or_else(|| {
                format!("reading the next batch failed (error code {code})")
            });
            return Some(Err(ArrowError::CDataInterface(reported)));
        }
        // A released array is how the producer says there are no more.
        if array.is_released() {
            return None;
        }
        self.repair(&mut array);

        let fields = self.schema.fields().clone();
        let data = match unsafe { from_ffi_and_data_type(array, DataType::Struct(fields)) } {
            Ok(data) => data,
            Err(err) => return Some(Err(err)),
        };
        let rows = data.len();
        Some(RecordBatch::try_new_with_options(
            Arc::clone(&self.schema),
            StructArray::from(data).into_parts().1,
            &RecordBatchOptions::new().with_row_count(Some(rows)),
        ))
    }
}

impl RecordBatchReader for RepairingReader {
    fn schema(&self) -> SchemaRef {
        Arc::clone(&self.schema)
    }
}
