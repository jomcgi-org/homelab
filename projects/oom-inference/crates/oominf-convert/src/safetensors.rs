//! Minimal reader for a sharded safetensors checkpoint (`model.safetensors.index.json`
//! plus shards). Shards are memory-mapped lazily; tensors are borrowed byte slices.

use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use anyhow::{Context, Result, bail};
use memmap2::Mmap;
use serde_json::Value;

pub struct TensorView<'a> {
    pub dtype: &'a str,
    pub shape: &'a [u64],
    pub bytes: &'a [u8],
}

struct Meta {
    dtype: String,
    shape: Vec<u64>,
    start: usize,
    end: usize,
}

struct Shard {
    path: PathBuf,
    map: OnceLock<(Mmap, usize, HashMap<String, Meta>)>,
}

impl Shard {
    fn load(&self) -> Result<&(Mmap, usize, HashMap<String, Meta>)> {
        if let Some(loaded) = self.map.get() {
            return Ok(loaded);
        }
        let file =
            File::open(&self.path).with_context(|| format!("open {}", self.path.display()))?;
        // SAFETY: release checkpoints are immutable while we convert them.
        let map =
            unsafe { Mmap::map(&file) }.with_context(|| format!("mmap {}", self.path.display()))?;
        let (data_start, metas) =
            parse_header(&map).with_context(|| format!("{}", self.path.display()))?;
        Ok(self.map.get_or_init(|| (map, data_start, metas)))
    }
}

fn parse_header(map: &[u8]) -> Result<(usize, HashMap<String, Meta>)> {
    if map.len() < 8 {
        bail!("file too short for a safetensors header");
    }
    let n = u64::from_le_bytes(map[..8].try_into().unwrap()) as usize;
    let data_start = 8 + n;
    if map.len() < data_start {
        bail!("header length {n} exceeds file size");
    }
    let header: BTreeMap<String, Value> = serde_json::from_slice(&map[8..data_start])?;
    let mut metas = HashMap::with_capacity(header.len());
    for (name, v) in header {
        if name == "__metadata__" {
            continue;
        }
        let dtype = v["dtype"].as_str().context("dtype")?.to_owned();
        let shape = v["shape"]
            .as_array()
            .context("shape")?
            .iter()
            .map(|d| d.as_u64().context("shape dim"))
            .collect::<Result<Vec<_>>>()?;
        let offs = v["data_offsets"].as_array().context("data_offsets")?;
        let (start, end) = (
            offs[0].as_u64().context("start")? as usize,
            offs[1].as_u64().context("end")? as usize,
        );
        if data_start + end > map.len() || start > end {
            bail!("{name}: data offsets out of range");
        }
        metas.insert(
            name,
            Meta {
                dtype,
                shape,
                start,
                end,
            },
        );
    }
    Ok((data_start, metas))
}

pub struct Checkpoint {
    dir: PathBuf,
    /// Tensor name to shard index, in index order.
    names: BTreeMap<String, usize>,
    shards: Vec<Shard>,
}

impl Checkpoint {
    pub fn open(dir: &Path) -> Result<Self> {
        let index_path = dir.join("model.safetensors.index.json");
        if !index_path.exists() {
            let shard = Shard {
                path: dir.join("model.safetensors"),
                map: OnceLock::new(),
            };
            let names = shard
                .load()?
                .2
                .keys()
                .map(|name| (name.clone(), 0))
                .collect();
            return Ok(Checkpoint {
                dir: dir.to_owned(),
                names,
                shards: vec![shard],
            });
        }
        let index: Value = serde_json::from_slice(
            &std::fs::read(&index_path)
                .with_context(|| format!("read {}", index_path.display()))?,
        )?;
        let map = index["weight_map"].as_object().context("weight_map")?;
        let mut files: Vec<String> = Vec::new();
        let mut file_ids: HashMap<String, usize> = HashMap::new();
        let mut names = BTreeMap::new();
        for (name, file) in map {
            let file = file.as_str().context("weight_map value")?;
            let id = *file_ids.entry(file.to_owned()).or_insert_with(|| {
                files.push(file.to_owned());
                files.len() - 1
            });
            names.insert(name.clone(), id);
        }
        let shards = files
            .iter()
            .map(|f| Shard {
                path: dir.join(f),
                map: OnceLock::new(),
            })
            .collect();
        Ok(Checkpoint {
            dir: dir.to_owned(),
            names,
            shards,
        })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Source tensor metadata used for the converted checkpoint fingerprint.
    pub fn metadata_bytes(&self) -> Result<Vec<u8>> {
        let path = self.dir.join("model.safetensors.index.json");
        if path.exists() {
            return Ok(std::fs::read(path)?);
        }
        let (map, start, _) = self.shards[0].load()?;
        Ok(map[..*start].to_vec())
    }

    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.names.keys().map(String::as_str)
    }

    pub fn len(&self) -> usize {
        self.names.len()
    }

    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    pub fn get(&self, name: &str) -> Result<TensorView<'_>> {
        let &shard = self
            .names
            .get(name)
            .with_context(|| format!("tensor {name} not in checkpoint"))?;
        let (map, data_start, metas) = self.shards[shard].load()?;
        let meta = metas
            .get(name)
            .with_context(|| format!("tensor {name} missing from its shard"))?;
        Ok(TensorView {
            dtype: &meta.dtype,
            shape: &meta.shape,
            bytes: &map[data_start + meta.start..data_start + meta.end],
        })
    }
}

/// A tensor read fully into memory from a single safetensors file.
pub struct OwnedTensor {
    pub dtype: String,
    pub shape: Vec<u64>,
    pub bytes: Vec<u8>,
}

/// Reads every tensor of one (small) safetensors file.
pub fn read_file(path: &Path) -> Result<HashMap<String, OwnedTensor>> {
    let data = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
    let (start, metas) = parse_header(&data).with_context(|| format!("{}", path.display()))?;
    Ok(metas
        .into_iter()
        .map(|(name, m)| {
            let bytes = data[start + m.start..start + m.end].to_vec();
            (
                name,
                OwnedTensor {
                    dtype: m.dtype,
                    shape: m.shape,
                    bytes,
                },
            )
        })
        .collect())
}
