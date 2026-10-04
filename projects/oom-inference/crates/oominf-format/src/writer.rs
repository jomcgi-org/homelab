use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

use crate::{
    ALIGN, EXPERTS_FILE, Error, ExpertGroup, FORMAT_TAG, FORMAT_VERSION, FileInfo, Files,
    INDEX_NAME, Index, RecordSchema, Result, Source, TensorEntry, TensorFile, align_up, checksum,
    io_err, tensor_nbytes,
};

/// Appends aligned, zero-padded blobs to one data file.
struct Segment {
    path: PathBuf,
    out: BufWriter<File>,
    len: u64,
}

impl Segment {
    fn create(path: PathBuf) -> Result<Self> {
        let file = File::create(&path).map_err(io_err(path.display()))?;
        Ok(Segment {
            out: BufWriter::with_capacity(8 << 20, file),
            path,
            len: 0,
        })
    }

    /// Writes `bytes` at the current (aligned) end and pads to [`ALIGN`].
    /// Returns the offset it was written at.
    fn append(&mut self, bytes: &[u8]) -> Result<u64> {
        let offset = self.len;
        let padded = align_up(bytes.len() as u64, ALIGN);
        let pad = (padded - bytes.len() as u64) as usize;
        self.out
            .write_all(bytes)
            .map_err(io_err(self.path.display()))?;
        self.out
            .write_all(&ZEROS[..pad])
            .map_err(io_err(self.path.display()))?;
        self.len += padded;
        Ok(offset)
    }

    fn finish(self) -> Result<FileInfo> {
        let file = self.out.into_inner().map_err(|e| Error::Io {
            path: self.path.display().to_string(),
            source: e.into_error(),
        })?;
        file.sync_all().map_err(io_err(self.path.display()))?;
        Ok(FileInfo { bytes: self.len })
    }
}

static ZEROS: [u8; ALIGN as usize] = [0; ALIGN as usize];

/// Writes a converted model directory. Tensors and expert groups are appended
/// in call order; [`Writer::finish`] writes `index.json` last, so a directory
/// without one is an incomplete conversion.
pub struct Writer {
    dir: PathBuf,
    dense: Segment,
    experts: Segment,
    tables: Segment,
    source: Source,
    tensors: Vec<TensorEntry>,
    groups: Vec<ExpertGroup>,
}

impl Writer {
    pub fn create(dir: &Path, source: Source) -> Result<Self> {
        std::fs::create_dir_all(dir).map_err(io_err(dir.display()))?;
        if dir.join(INDEX_NAME).exists() {
            return Err(Error::Usage(format!(
                "{} already holds a converted model",
                dir.display()
            )));
        }
        Ok(Writer {
            dense: Segment::create(dir.join(TensorFile::Dense.file_name()))?,
            experts: Segment::create(dir.join(EXPERTS_FILE))?,
            tables: Segment::create(dir.join(TensorFile::Tables.file_name()))?,
            dir: dir.to_owned(),
            source,
            tensors: Vec::new(),
            groups: Vec::new(),
        })
    }

    pub fn add_tensor(
        &mut self,
        file: TensorFile,
        name: &str,
        dtype: &str,
        shape: &[u64],
        bytes: &[u8],
    ) -> Result<()> {
        let nbytes = tensor_nbytes(dtype, shape)?;
        if nbytes != bytes.len() as u64 {
            return Err(Error::Usage(format!(
                "{name}: {dtype}{shape:?} is {nbytes} bytes, got {}",
                bytes.len()
            )));
        }
        let segment = match file {
            TensorFile::Dense => &mut self.dense,
            TensorFile::Tables => &mut self.tables,
        };
        let offset = segment.append(bytes)?;
        self.tensors.push(TensorEntry {
            name: name.to_owned(),
            file,
            dtype: dtype.to_owned(),
            shape: shape.to_vec(),
            offset,
            nbytes,
            xxh3: checksum(bytes),
        });
        Ok(())
    }

    /// Writes one layer's expert records. `fill(expert, record)` receives a
    /// zeroed `stride`-byte buffer and must copy each part to its offset.
    pub fn add_expert_group(
        &mut self,
        layer: u32,
        num_experts: u32,
        schema: RecordSchema,
        mut fill: impl FnMut(u32, &mut [u8]) -> Result<()>,
    ) -> Result<()> {
        if self.groups.iter().any(|g| g.layer == layer) {
            return Err(Error::Usage(format!(
                "expert group for layer {layer} written twice"
            )));
        }
        let mut record = vec![0u8; schema.stride as usize];
        let offset = self.experts.len;
        let mut sums = Vec::with_capacity(num_experts as usize);
        for expert in 0..num_experts {
            record.fill(0);
            fill(expert, &mut record)?;
            sums.push(checksum(&record));
            self.experts.append(&record)?;
        }
        self.groups.push(ExpertGroup {
            layer,
            num_experts,
            offset,
            schema,
            xxh3: sums,
        });
        Ok(())
    }

    pub fn finish(self) -> Result<Index> {
        let index = Index {
            format: FORMAT_TAG.to_owned(),
            version: FORMAT_VERSION,
            source: self.source,
            files: Files {
                dense: self.dense.finish()?,
                experts: self.experts.finish()?,
                tables: self.tables.finish()?,
            },
            tensors: self.tensors,
            expert_groups: self.groups,
        };
        let path = self.dir.join(INDEX_NAME);
        let tmp = self.dir.join(format!("{INDEX_NAME}.tmp"));
        let json = serde_json::to_vec_pretty(&index).map_err(|e| Error::Index(e.to_string()))?;
        std::fs::write(&tmp, json).map_err(io_err(tmp.display()))?;
        std::fs::rename(&tmp, &path).map_err(io_err(path.display()))?;
        Ok(index)
    }
}

/// Copies `bytes` into `record` at the offset of the schema part `name`.
pub fn put_part(schema: &RecordSchema, record: &mut [u8], name: &str, bytes: &[u8]) -> Result<()> {
    let part = schema
        .part(name)
        .ok_or_else(|| Error::Usage(format!("schema {} has no part {name}", schema.layout)))?;
    if part.nbytes != bytes.len() as u64 {
        return Err(Error::Usage(format!(
            "part {name}: expected {} bytes, got {}",
            part.nbytes,
            bytes.len()
        )));
    }
    let start = part.offset as usize;
    record[start..start + bytes.len()].copy_from_slice(bytes);
    Ok(())
}
