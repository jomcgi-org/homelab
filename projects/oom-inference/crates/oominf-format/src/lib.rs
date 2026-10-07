//! The oominf on-disk weight format (see `docs/dev/format.md`).
//!
//! A converted model is a directory holding `index.json` plus three data files:
//! `dense.bin`, `experts.bin` and `tables.bin`. Every tensor and expert record
//! starts on an [`ALIGN`] boundary so it can be read with direct I/O, and carries
//! an xxh3-64 checksum. Routed experts are stored one contiguous record per
//! (layer, expert), so a cache miss is a single read.

mod reader;
mod writer;

use serde::{Deserialize, Serialize};

pub use reader::{Model, VerifyReport};
pub use writer::{Writer, put_part};

pub const FORMAT_TAG: &str = "oominf";
pub const FORMAT_VERSION: u32 = 0;
pub const INDEX_NAME: &str = "index.json";

/// File-level alignment of every tensor, record and table.
pub const ALIGN: u64 = 4096;
/// Alignment of each part inside an expert record.
pub const PART_ALIGN: u64 = 256;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("io error on {path}: {source}")]
    Io {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("bad index: {0}")]
    Index(String),
    #[error("unsupported dtype {0}")]
    Dtype(String),
    #[error("{0}")]
    Usage(String),
}

pub type Result<T> = std::result::Result<T, Error>;

pub(crate) fn io_err(path: impl std::fmt::Display) -> impl FnOnce(std::io::Error) -> Error {
    move |source| Error::Io {
        path: path.to_string(),
        source,
    }
}

pub fn align_up(n: u64, to: u64) -> u64 {
    n.div_ceil(to) * to
}

/// Size in bytes of one element of a safetensors-spelled dtype.
pub fn dtype_size(dtype: &str) -> Result<u64> {
    Ok(match dtype {
        "BOOL" | "U8" | "I8" | "F8_E4M3" | "F8_E5M2" => 1,
        "BF16" | "F16" | "I16" | "U16" => 2,
        "F32" | "I32" | "U32" => 4,
        "F64" | "I64" | "U64" => 8,
        other => return Err(Error::Dtype(other.to_owned())),
    })
}

pub fn tensor_nbytes(dtype: &str, shape: &[u64]) -> Result<u64> {
    Ok(dtype_size(dtype)? * shape.iter().product::<u64>())
}

pub fn checksum(bytes: &[u8]) -> String {
    format!("{:016x}", xxhash_rust::xxh3::xxh3_64(bytes))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TensorFile {
    Dense,
    Tables,
}

impl TensorFile {
    pub fn file_name(self) -> &'static str {
        match self {
            TensorFile::Dense => "dense.bin",
            TensorFile::Tables => "tables.bin",
        }
    }
}

pub const EXPERTS_FILE: &str = "experts.bin";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Index {
    pub format: String,
    pub version: u32,
    pub source: Source,
    pub files: Files,
    pub tensors: Vec<TensorEntry>,
    pub expert_groups: Vec<ExpertGroup>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Source {
    pub path: String,
    pub model_type: String,
    /// Free-form provenance, e.g. upstream revision or checkpoint fingerprint.
    #[serde(default)]
    pub fingerprint: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Files {
    pub dense: FileInfo,
    pub experts: FileInfo,
    pub tables: FileInfo,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct FileInfo {
    pub bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TensorEntry {
    pub name: String,
    pub file: TensorFile,
    pub dtype: String,
    pub shape: Vec<u64>,
    pub offset: u64,
    pub nbytes: u64,
    pub xxh3: String,
}

/// One part of an expert record.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Part {
    pub name: String,
    pub dtype: String,
    pub shape: Vec<u64>,
    /// Offset within the record, a multiple of [`PART_ALIGN`].
    pub offset: u64,
    pub nbytes: u64,
}

/// The shared layout of every record in an expert group.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RecordSchema {
    pub layout: String,
    pub parts: Vec<Part>,
    pub stride: u64,
}

impl RecordSchema {
    /// Lays parts out in order, each at a [`PART_ALIGN`] offset; the stride is
    /// the total rounded up to [`ALIGN`].
    pub fn new(layout: &str, parts: &[(&str, &str, &[u64])]) -> Result<Self> {
        let mut offset = 0;
        let mut laid = Vec::with_capacity(parts.len());
        for &(name, dtype, shape) in parts {
            let nbytes = tensor_nbytes(dtype, shape)?;
            laid.push(Part {
                name: name.to_owned(),
                dtype: dtype.to_owned(),
                shape: shape.to_vec(),
                offset,
                nbytes,
            });
            offset = align_up(offset + nbytes, PART_ALIGN);
        }
        Ok(RecordSchema {
            layout: layout.to_owned(),
            parts: laid,
            stride: align_up(offset, ALIGN),
        })
    }

    pub fn part(&self, name: &str) -> Option<&Part> {
        self.parts.iter().find(|p| p.name == name)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExpertGroup {
    pub layer: u32,
    pub num_experts: u32,
    /// Offset of record 0 in `experts.bin`.
    pub offset: u64,
    #[serde(flatten)]
    pub schema: RecordSchema,
    /// Checksum of each record's full `stride` bytes, padding included.
    pub xxh3: Vec<String>,
}

impl ExpertGroup {
    pub fn record_offset(&self, expert: u32) -> u64 {
        self.offset + u64::from(expert) * self.schema.stride
    }
}

#[cfg(test)]
mod tests;
