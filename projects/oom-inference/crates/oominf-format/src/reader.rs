use std::collections::HashMap;
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};

use crate::{
    EXPERTS_FILE, Error, ExpertGroup, FORMAT_TAG, FORMAT_VERSION, INDEX_NAME, Index, Result,
    TensorEntry, TensorFile, checksum, io_err,
};

/// A converted model opened for reading.
///
/// Reads are positional (`pread`), so a `Model` can be shared across threads.
/// Direct-I/O loaders use [`Model::record_location`] / [`TensorEntry`] offsets
/// with their own aligned buffers instead.
pub struct Model {
    dir: PathBuf,
    index: Index,
    dense: File,
    experts: File,
    tables: File,
    by_name: HashMap<String, usize>,
    by_layer: HashMap<u32, usize>,
}

#[derive(Debug, Default)]
pub struct VerifyReport {
    pub tensors_checked: usize,
    pub records_checked: usize,
    pub bytes_checked: u64,
    pub mismatches: Vec<String>,
}

impl Model {
    pub fn open(dir: &Path) -> Result<Self> {
        let index_path = dir.join(INDEX_NAME);
        let raw = std::fs::read(&index_path).map_err(io_err(index_path.display()))?;
        let index: Index = serde_json::from_slice(&raw).map_err(|e| Error::Index(e.to_string()))?;
        if index.format != FORMAT_TAG || index.version != FORMAT_VERSION {
            return Err(Error::Index(format!(
                "unsupported format {} v{} (want {FORMAT_TAG} v{FORMAT_VERSION})",
                index.format, index.version
            )));
        }
        let open = |name: &str, expect: u64| -> Result<File> {
            let path = dir.join(name);
            let file = File::open(&path).map_err(io_err(path.display()))?;
            let len = file.metadata().map_err(io_err(path.display()))?.len();
            if len != expect {
                return Err(Error::Index(format!(
                    "{name} is {len} bytes, index says {expect}"
                )));
            }
            Ok(file)
        };
        let dense = open(TensorFile::Dense.file_name(), index.files.dense.bytes)?;
        let experts = open(EXPERTS_FILE, index.files.experts.bytes)?;
        let tables = open(TensorFile::Tables.file_name(), index.files.tables.bytes)?;
        let by_name = index
            .tensors
            .iter()
            .enumerate()
            .map(|(i, t)| (t.name.clone(), i))
            .collect();
        let by_layer = index
            .expert_groups
            .iter()
            .enumerate()
            .map(|(i, g)| (g.layer, i))
            .collect();
        Ok(Model {
            dir: dir.to_owned(),
            index,
            dense,
            experts,
            tables,
            by_name,
            by_layer,
        })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn index(&self) -> &Index {
        &self.index
    }

    pub fn tensor(&self, name: &str) -> Option<&TensorEntry> {
        self.by_name.get(name).map(|&i| &self.index.tensors[i])
    }

    pub fn expert_group(&self, layer: u32) -> Option<&ExpertGroup> {
        self.by_layer
            .get(&layer)
            .map(|&i| &self.index.expert_groups[i])
    }

    /// `(offset, stride)` of one expert record in `experts.bin`.
    pub fn record_location(&self, layer: u32, expert: u32) -> Result<(u64, u64)> {
        let group = self
            .expert_group(layer)
            .ok_or_else(|| Error::Usage(format!("no expert group for layer {layer}")))?;
        if expert >= group.num_experts {
            return Err(Error::Usage(format!(
                "expert {expert} out of range for layer {layer} ({} experts)",
                group.num_experts
            )));
        }
        Ok((group.record_offset(expert), group.schema.stride))
    }

    pub fn read_tensor(&self, entry: &TensorEntry) -> Result<Vec<u8>> {
        let mut buf = vec![0u8; entry.nbytes as usize];
        self.file(entry.file)
            .read_exact_at(&mut buf, entry.offset)
            .map_err(io_err(&entry.name))?;
        Ok(buf)
    }

    /// Reads one full record (`stride` bytes) into `buf`.
    pub fn read_record(&self, layer: u32, expert: u32, buf: &mut [u8]) -> Result<()> {
        let (offset, stride) = self.record_location(layer, expert)?;
        if buf.len() as u64 != stride {
            return Err(Error::Usage(format!(
                "record buffer is {} bytes, stride {stride}",
                buf.len()
            )));
        }
        self.experts
            .read_exact_at(buf, offset)
            .map_err(io_err(format!("layer {layer} expert {expert}")))
    }

    /// Recomputes every checksum. `progress(done_bytes)` is called after each item.
    pub fn verify(&self, mut progress: impl FnMut(u64)) -> Result<VerifyReport> {
        let mut report = VerifyReport::default();
        for entry in &self.index.tensors {
            let bytes = self.read_tensor(entry)?;
            if checksum(&bytes) != entry.xxh3 {
                report.mismatches.push(format!("tensor {}", entry.name));
            }
            report.tensors_checked += 1;
            report.bytes_checked += entry.nbytes;
            progress(report.bytes_checked);
        }
        for group in &self.index.expert_groups {
            let mut buf = vec![0u8; group.schema.stride as usize];
            for expert in 0..group.num_experts {
                self.read_record(group.layer, expert, &mut buf)?;
                if checksum(&buf) != group.xxh3[expert as usize] {
                    report
                        .mismatches
                        .push(format!("layer {} expert {expert}", group.layer));
                }
                report.records_checked += 1;
                report.bytes_checked += group.schema.stride;
                progress(report.bytes_checked);
            }
        }
        Ok(report)
    }

    fn file(&self, which: TensorFile) -> &File {
        match which {
            TensorFile::Dense => &self.dense,
            TensorFile::Tables => &self.tables,
        }
    }
}
