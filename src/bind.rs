//! Arrow values on their way into a statement's parameters.
//!
//! The mirror of `columns`: that turns SQLite's storage classes into Arrow
//! arrays, this turns Arrow arrays into values SQLite can bind. Arrow has many
//! more types than SQLite has storage classes, so each column is first cast to
//! whichever of the five it belongs in -- once per column, not once per value
//! -- and read from there.
//!
//! ```text
//! NULL      INTEGER                     REAL             TEXT        BLOB
//! null      int8/16/32/64               float16/32/64    utf8        binary
//!           uint8/16/32                                  largeutf8   largebinary
//!           boolean                                      utf8view    binaryview
//!                                                                    fixedsizebinary
//! ```
//!
//! Not uint64: SQLite's integers are signed, so anything above `i64::MAX`
//! could only be stored by losing either its value or its type. A caller who
//! has one knows better than this code what it should become.
//!
//! Dictionary columns are bound as whatever they encode.

use crate::columns::Cell;
use arrow_array::cast::AsArray;
use arrow_array::{Array, ArrayRef};
use arrow_schema::DataType;

/// Which of SQLite's storage classes `source` is bound as, or `None` when it
/// has no obvious place in any of them.
pub fn target_type(source: &DataType) -> Option<DataType> {
    Some(match source {
        DataType::Null => DataType::Null,
        DataType::Boolean
        | DataType::Int8
        | DataType::Int16
        | DataType::Int32
        | DataType::Int64
        | DataType::UInt8
        | DataType::UInt16
        | DataType::UInt32 => DataType::Int64,
        DataType::Float16 | DataType::Float32 | DataType::Float64 => DataType::Float64,
        DataType::Utf8 | DataType::LargeUtf8 | DataType::Utf8View => DataType::Utf8View,
        DataType::Binary
        | DataType::LargeBinary
        | DataType::BinaryView
        | DataType::FixedSizeBinary(_) => DataType::BinaryView,
        // Bound as whatever it encodes; the keys are an encoding, not a type.
        DataType::Dictionary(_, values) => return target_type(values),
        _ => return None,
    })
}

/// One value of an already-cast column.
///
/// Only the five types `target_type` produces reach here, so anything else is
/// a bug in the caller rather than bad input.
pub fn cell_at(column: &ArrayRef, row: usize) -> Cell<'_> {
    if column.is_null(row) {
        return Cell::Null;
    }
    match column.data_type() {
        DataType::Null => Cell::Null,
        DataType::Int64 => Cell::Int(column.as_primitive::<arrow_array::types::Int64Type>().value(row)),
        DataType::Float64 => {
            Cell::Real(column.as_primitive::<arrow_array::types::Float64Type>().value(row))
        }
        DataType::Utf8View => Cell::Text(column.as_string_view().value(row)),
        DataType::BinaryView => Cell::Blob(column.as_binary_view().value(row)),
        other => unreachable!("{other} was not cast before binding"),
    }
}
