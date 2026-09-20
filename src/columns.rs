//! Column-at-a-time accumulation of SQLite values into Arrow arrays.
//!
//! SQLite is dynamically typed: a column's storage class is a property of each
//! value, not of the column, so a single result column can yield INTEGER on one
//! row and TEXT on the next. Arrow needs one type for the whole column.
//!
//! Each column is therefore built optimistically, in one contiguous typed
//! buffer, on the assumption that what has arrived so far is what will keep
//! arriving. If a wider value does appear, the accumulated buffer is promoted
//! once and building continues. The promotion order is SQLite's own storage
//! class order:
//!
//! ```text
//! NULL  <  INTEGER  <  REAL  <  TEXT  <  BLOB
//! ```
//!
//! NULL is the identity: it never promotes anything, it only contributes a null
//! slot. Promotion is one-way, so a column is promoted at most four times
//! regardless of how many rows it has.

use arrow_array::builder::{BinaryBuilder, Float64Builder, Int64Builder, StringBuilder};
use arrow_array::{Array, ArrayRef, BinaryArray, Float64Array, Int64Array, NullArray, StringArray};
use arrow_schema::{DataType, Field};
use std::sync::Arc;

/// One SQLite value, borrowed from the statement that produced it.
#[derive(Debug, Clone, Copy)]
pub enum Cell<'a> {
    Null,
    Int(i64),
    Real(f64),
    Text(&'a str),
    Blob(&'a [u8]),
}

impl Cell<'_> {
    /// Position in the promotion order above. NULL is 0 and never forces a
    /// promotion, since every builder can represent a null.
    fn rank(&self) -> u8 {
        match self {
            Cell::Null => 0,
            Cell::Int(_) => 1,
            Cell::Real(_) => 2,
            Cell::Text(_) => 3,
            Cell::Blob(_) => 4,
        }
    }
}

/// A column being accumulated, currently holding values of one storage class.
pub enum ColumnBuilder {
    /// Nothing but NULLs so far, so no value type has been chosen yet.
    Null(usize),
    Int(Int64Builder),
    Real(Float64Builder),
    Text(StringBuilder),
    Blob(BinaryBuilder),
}

impl Default for ColumnBuilder {
    fn default() -> Self {
        ColumnBuilder::Null(0)
    }
}

impl ColumnBuilder {
    fn rank(&self) -> u8 {
        match self {
            ColumnBuilder::Null(_) => 0,
            ColumnBuilder::Int(_) => 1,
            ColumnBuilder::Real(_) => 2,
            ColumnBuilder::Text(_) => 3,
            ColumnBuilder::Blob(_) => 4,
        }
    }

    pub fn push(&mut self, cell: Cell<'_>) {
        if cell.rank() > self.rank() {
            self.promote_to(cell.rank());
        }
        self.append(cell);
    }

    /// Append into the current type, widening the value as needed. Only called
    /// once the builder is known to be at least as wide as the value.
    fn append(&mut self, cell: Cell<'_>) {
        match (self, cell) {
            (ColumnBuilder::Null(nulls), _) => *nulls += 1,

            (ColumnBuilder::Int(b), Cell::Null) => b.append_null(),
            (ColumnBuilder::Int(b), Cell::Int(v)) => b.append_value(v),

            (ColumnBuilder::Real(b), Cell::Null) => b.append_null(),
            // Exact up to 2^53, as everywhere else that mixes the two.
            (ColumnBuilder::Real(b), Cell::Int(v)) => b.append_value(v as f64),
            (ColumnBuilder::Real(b), Cell::Real(v)) => b.append_value(v),

            (ColumnBuilder::Text(b), Cell::Null) => b.append_null(),
            (ColumnBuilder::Text(b), cell) => b.append_value(text_of(cell)),

            (ColumnBuilder::Blob(b), Cell::Null) => b.append_null(),
            (ColumnBuilder::Blob(b), Cell::Blob(v)) => b.append_value(v),
            (ColumnBuilder::Blob(b), cell) => b.append_value(text_of(cell).as_bytes()),

            // Unreachable: push() promotes first, so the builder is never
            // narrower than the value by the time we get here.
            (b, cell) => unreachable!("{:?} into a narrower builder", cell.rank().min(b.rank())),
        }
    }

    /// Rebuild as `rank`, replaying what has accumulated so far through
    /// `append`, so a value converted during promotion and one appended after
    /// it go through exactly the same conversion.
    fn promote_to(&mut self, rank: u8) {
        let old = std::mem::take(self);
        let mut new = match rank {
            1 => ColumnBuilder::Int(Int64Builder::new()),
            2 => ColumnBuilder::Real(Float64Builder::new()),
            3 => ColumnBuilder::Text(StringBuilder::new()),
            _ => ColumnBuilder::Blob(BinaryBuilder::new()),
        };
        old.replay_into(&mut new);
        *self = new;
    }

    /// Feed every value accumulated so far into `target`.
    fn replay_into(self, target: &mut ColumnBuilder) {
        match self {
            ColumnBuilder::Null(nulls) => {
                for _ in 0..nulls {
                    target.append(Cell::Null);
                }
            }
            ColumnBuilder::Int(mut b) => replay(&b.finish(), target, |a: &Int64Array, i| Cell::Int(a.value(i))),
            ColumnBuilder::Real(mut b) => {
                replay(&b.finish(), target, |a: &Float64Array, i| Cell::Real(a.value(i)));
            }
            ColumnBuilder::Text(mut b) => replay(&b.finish(), target, |a: &StringArray, i| Cell::Text(a.value(i))),
            ColumnBuilder::Blob(mut b) => replay(&b.finish(), target, |a: &BinaryArray, i| Cell::Blob(a.value(i))),
        }
    }

    /// The finished column, plus the Arrow field describing it.
    pub fn finish(self, name: &str) -> (ArrayRef, Field) {
        let (array, data_type): (ArrayRef, DataType) = match self {
            // A column of nothing but NULLs has no value type to infer.
            ColumnBuilder::Null(nulls) => (Arc::new(NullArray::new(nulls)), DataType::Null),
            ColumnBuilder::Int(mut b) => (Arc::new(b.finish()), DataType::Int64),
            ColumnBuilder::Real(mut b) => (Arc::new(b.finish()), DataType::Float64),
            ColumnBuilder::Text(mut b) => (Arc::new(b.finish()), DataType::Utf8),
            ColumnBuilder::Blob(mut b) => (Arc::new(b.finish()), DataType::Binary),
        };
        (array, Field::new(name, data_type, true))
    }
}

fn replay<A: Array + 'static>(array: &A, target: &mut ColumnBuilder, cell: impl Fn(&A, usize) -> Cell<'_>) {
    for i in 0..array.len() {
        if array.is_null(i) {
            target.append(Cell::Null);
        } else {
            target.append(cell(array, i));
        }
    }
}

/// How a value reads once its column has been promoted to TEXT.
///
/// Integers, and reals with no fractional part, match `CAST(x AS TEXT)`: 1
/// renders "1" and 2.0 renders "2.0" rather than losing the ".0". Other reals
/// use Rust's shortest representation that round-trips, which agrees with
/// SQLite for ordinary values but not everywhere -- SQLite's own `%!.15g`
/// prints 1e20 as "1.0e+20" where this gives "100000000000000000000.0", emits
/// 0.33333333333333332 where this gives 0.3333333333333333, and normalises
/// -0.0 to "0.0". Matching it exactly would mean reimplementing that
/// formatter; these arrays are not claimed to be byte-identical to CAST.
fn text_of(cell: Cell<'_>) -> String {
    match cell {
        Cell::Null => String::new(),
        Cell::Int(v) => v.to_string(),
        Cell::Real(v) => {
            if v.is_finite() && v == v.trunc() {
                format!("{v:.1}")
            } else {
                format!("{v}")
            }
        }
        Cell::Text(v) => v.to_string(),
        Cell::Blob(v) => String::from_utf8_lossy(v).into_owned(),
    }
}
