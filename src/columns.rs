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
/// to get anywhere. Tens of kilobytes, against a result that turns out to be
/// small.
const INITIAL_CAPACITY: usize = 4096;

/// Rows per chunk.
///
/// A column is not one buffer that grows; it is a run of chunks this long.
/// Growing meant reallocating and copying everything so far, over and over,
/// which is cheap where the allocator can extend a mapping in place and
/// expensive where it cannot. A chunk is allocated once, filled, and handed
/// over.
pub const CHUNK_ROWS: usize = 65_536;

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
        let mut new = Self::with_rank(rank, INITIAL_CAPACITY.max(old.len()));
        old.replay_into(&mut new);
        *self = new;
    }

    /// An empty builder for `rank`, sized for `capacity` rows.
    fn with_rank(rank: u8, capacity: usize) -> Self {
        match rank {
            0 => ColumnBuilder::Null(0),
            1 => ColumnBuilder::Int(Int64Builder::with_capacity(capacity)),
            2 => ColumnBuilder::Real(Float64Builder::with_capacity(capacity)),
            3 => ColumnBuilder::Text(StringViewBuilder::with_capacity(capacity)),
            _ => ColumnBuilder::Blob(BinaryViewBuilder::with_capacity(capacity)),
        }
    }

    /// The Arrow type a builder of `rank` produces.
    fn data_type(rank: u8) -> DataType {
        match rank {
            0 => DataType::Null,
            1 => DataType::Int64,
            2 => DataType::Float64,
            3 => DataType::Utf8View,
            _ => DataType::BinaryView,
        }
    }

    /// Take what has accumulated as a chunk, leaving the builder empty, of the
    /// same type, and sized for the next one.
    fn take(&mut self) -> ArrayRef {
        match self {
            ColumnBuilder::Null(nulls) => {
                let array = Arc::new(NullArray::new(*nulls));
                *nulls = 0;
                array
            }
            ColumnBuilder::Int(b) => {
                let array = Arc::new(b.finish());
                *b = Int64Builder::with_capacity(CHUNK_ROWS);
                array
            }
            ColumnBuilder::Real(b) => {
                let array = Arc::new(b.finish());
                *b = Float64Builder::with_capacity(CHUNK_ROWS);
                array
            }
            ColumnBuilder::Text(b) => {
                let array = Arc::new(b.finish());
                *b = StringViewBuilder::with_capacity(CHUNK_ROWS);
                array
            }
            ColumnBuilder::Blob(b) => {
                let array = Arc::new(b.finish());
                *b = BinaryViewBuilder::with_capacity(CHUNK_ROWS);
                array
            }
        }
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

}

/// One result column, accumulated as a run of chunks.
pub struct Column {
    name: String,
    /// Finished chunks, each with the promotion rank it was built at. A value
    /// wide enough to promote the column arrives long after earlier chunks
    /// have been closed, so they can disagree until `finish` settles them.
    chunks: Vec<(u8, ArrayRef)>,
    builder: ColumnBuilder,
    pending: usize,
}

impl Column {
    pub fn new(name: String) -> Self {
        Column {
            name,
            chunks: Vec::new(),
            builder: ColumnBuilder::default(),
            pending: 0,
        }
    }

    #[inline]
    pub fn push(&mut self, cell: Cell<'_>) {
        self.builder.push(cell);
        self.pending += 1;
        if self.pending == CHUNK_ROWS {
            self.close_chunk();
        }
    }

    #[inline(never)]
    fn close_chunk(&mut self) {
        let rank = self.builder.rank();
        self.chunks.push((rank, self.builder.take()));
        self.pending = 0;
    }

    /// The column's chunks, all of one type, and the field describing them.
    ///
    /// Chunks closed before a promotion hold a narrower type than the ones
    /// after it, and an Arrow column is one type throughout. The narrow ones
    /// are replayed through the same `append` an in-stream promotion uses, so
    /// a value converted here and one converted there come out identical.
    pub fn finish(mut self) -> (Vec<ArrayRef>, Field) {
        // Unconditionally, so a column that saw no rows still has a chunk and
        // reports a length of zero rather than nothing at all.
        self.close_chunk();
        let rank = self.chunks.iter().map(|(rank, _)| *rank).max().unwrap_or(0);

        let chunks = self
            .chunks
            .into_iter()
            .map(|(chunk_rank, array)| {
                if chunk_rank == rank {
                    array
                } else {
                    promote_array(&array, rank)
                }
            })
            .collect();
        (chunks, Field::new(self.name, ColumnBuilder::data_type(rank), true))
    }
}

/// Rebuild a closed chunk at a wider rank, value by value.
fn promote_array(array: &ArrayRef, rank: u8) -> ArrayRef {
    let mut target = ColumnBuilder::with_rank(rank, array.len());
    match array.data_type() {
        DataType::Null => {
            for _ in 0..array.len() {
                target.append(Cell::Null);
            }
        }
        DataType::Int64 => replay(as_array::<Int64Array>(array), &mut target, |a, i| Cell::Int(a.value(i))),
        DataType::Float64 => replay(as_array::<Float64Array>(array), &mut target, |a, i| Cell::Real(a.value(i))),
        DataType::Utf8View => replay(as_array::<StringViewArray>(array), &mut target, |a, i| Cell::Text(a.value(i))),
        // Nothing is wider than BinaryView, so it is never the one promoted.
        _ => unreachable!("{:?} cannot be promoted", array.data_type()),
    }
    target.take()
}

fn as_array<A: Array + 'static>(array: &ArrayRef) -> &A {
    array
        .as_any()
        .downcast_ref::<A>()
        .expect("chunk's array matches the type it was built at")
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
