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
//!
//! TEXT and BLOB build Arrow's view layouts rather than the older
//! offset-and-data ones, because that is what polars (and DataFusion, and
//! arrow-rs itself) hold strings in: handing over anything else costs the
//! consumer a conversion of the whole column.

use arrow_array::builder::{ArrayBuilder, BinaryViewBuilder, Float64Builder, Int64Builder, StringViewBuilder};
use arrow_array::{Array, ArrayRef, BinaryViewArray, Float64Array, Int64Array, NullArray, StringViewArray};
use arrow_schema::{DataType, Field};
use std::sync::Arc;

/// Rows a column is sized for the moment it gets a type.
///
/// Promotion is the exception rather than the rule -- a STRICT table never
/// promotes at all -- so a builder that exists is almost always one that will
/// go on being filled, and starting it at one row means a run of reallocations
/// to get anywhere.
const INITIAL_CAPACITY: usize = 4096;

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
    Text(StringViewBuilder),
    Blob(BinaryViewBuilder),
}

impl Default for ColumnBuilder {
    fn default() -> Self {
        ColumnBuilder::Null(0)
    }
}

impl ColumnBuilder {
    fn len(&self) -> usize {
        match self {
            ColumnBuilder::Null(nulls) => *nulls,
            ColumnBuilder::Int(b) => b.len(),
            ColumnBuilder::Real(b) => b.len(),
            ColumnBuilder::Text(b) => b.len(),
            ColumnBuilder::Blob(b) => b.len(),
        }
    }

    fn rank(&self) -> u8 {
        match self {
            ColumnBuilder::Null(_) => 0,
            ColumnBuilder::Int(_) => 1,
            ColumnBuilder::Real(_) => 2,
            ColumnBuilder::Text(_) => 3,
            ColumnBuilder::Blob(_) => 4,
        }
    }

    /// Append `cell` to the column, widening the column first if it has to.
    ///
    /// The type-stable case -- a value of exactly the type the column is
    /// already holding -- is the one this module is built around, so it is a
    /// short match this inlines into the caller's row loop. Everything else
    /// goes out of line.
    #[inline]
    pub fn push(&mut self, cell: Cell<'_>) {
        match (&mut *self, cell) {
            (ColumnBuilder::Int(b), Cell::Int(v)) => b.append_value(v),
            (ColumnBuilder::Real(b), Cell::Real(v)) => b.append_value(v),
            (ColumnBuilder::Text(b), Cell::Text(v)) => b.append_value(v),
            (ColumnBuilder::Blob(b), Cell::Blob(v)) => b.append_value(v),
            _ => self.push_widening(cell),
        }
    }

    /// A NULL, a value narrower than the column, or one wide enough to
    /// promote it.
    #[inline(never)]
    fn push_widening(&mut self, cell: Cell<'_>) {
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
        // Room for what is about to be replayed, on top of the head start.
        let capacity = INITIAL_CAPACITY.max(old.len());
        let mut new = match rank {
            1 => ColumnBuilder::Int(Int64Builder::with_capacity(capacity)),
            2 => ColumnBuilder::Real(Float64Builder::with_capacity(capacity)),
            3 => ColumnBuilder::Text(StringViewBuilder::with_capacity(capacity)),
            _ => ColumnBuilder::Blob(BinaryViewBuilder::with_capacity(capacity)),
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
            ColumnBuilder::Text(mut b) => replay(&b.finish(), target, |a: &StringViewArray, i| Cell::Text(a.value(i))),
            ColumnBuilder::Blob(mut b) => replay(&b.finish(), target, |a: &BinaryViewArray, i| Cell::Blob(a.value(i))),
        }
    }

    /// The finished column, plus the Arrow field describing it.
    pub fn finish(self, name: &str) -> (ArrayRef, Field) {
        let (array, data_type): (ArrayRef, DataType) = match self {
            // A column of nothing but NULLs has no value type to infer.
            ColumnBuilder::Null(nulls) => (Arc::new(NullArray::new(nulls)), DataType::Null),
            ColumnBuilder::Int(mut b) => (Arc::new(b.finish()), DataType::Int64),
            ColumnBuilder::Real(mut b) => (Arc::new(b.finish()), DataType::Float64),
            ColumnBuilder::Text(mut b) => (Arc::new(b.finish()), DataType::Utf8View),
            ColumnBuilder::Blob(mut b) => (Arc::new(b.finish()), DataType::BinaryView),
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
/// Numbers use Rust's shortest representation that round-trips. That is not
/// character-for-character what `CAST(x AS TEXT)` produces -- SQLite has its
/// own float formatter -- and deliberately makes no attempt to be.
fn text_of(cell: Cell<'_>) -> String {
    match cell {
        Cell::Null => String::new(),
        Cell::Int(v) => v.to_string(),
        Cell::Real(v) => v.to_string(),
        Cell::Text(v) => v.to_string(),
        Cell::Blob(v) => String::from_utf8_lossy(v).into_owned(),
    }
}
