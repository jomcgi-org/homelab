//! Converts an upstream release checkpoint (sharded safetensors) into the oominf
//! weight format. Conversion only re-lays bytes out; it never changes precision.

pub mod adapters;
pub mod safetensors;

use std::collections::BTreeMap;
use std::ops::RangeInclusive;
use std::path::Path;

use anyhow::{Context, Result, bail};
use oominf_format::{Index, Source, TensorFile, Writer, checksum};

use adapters::Class;
use safetensors::Checkpoint;

/// Non-weight files copied verbatim when present.
const METADATA_FILES: &[&str] = &[
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "vocab.json",
    "merges.txt",
    "hf_quant_config.json",
    "README.md",
];

#[derive(Debug, Clone)]
pub struct Options {
    /// Convert routed experts only for these layers (all when `None`).
    pub expert_layers: Option<RangeInclusive<u32>>,
    /// Write gather tables (they can be very large).
    pub tables: bool,
    /// Upstream checkpoint recorded in the index, e.g. `org/repo@revision`
    /// (default: see [`default_origin`]).
    pub origin: Option<String>,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            expert_layers: None,
            tables: true,
            origin: None,
        }
    }
}

#[derive(Debug, Default)]
pub struct Summary {
    pub dense: usize,
    pub tables: usize,
    pub expert_layers: usize,
    pub skipped: usize,
    pub excluded: usize,
}

pub fn convert(
    src: &Path,
    out: &Path,
    opts: &Options,
    mut log: impl FnMut(&str),
) -> Result<(Index, Summary)> {
    let config_raw = std::fs::read(src.join("config.json")).context("read config.json")?;
    let config: serde_json::Value = serde_json::from_slice(&config_raw)?;
    let adapter = adapters::for_config(&config)?;
    let ckpt = Checkpoint::open(src)?;
    let index_raw = std::fs::read(src.join("model.safetensors.index.json"))?;

    let mut dense = Vec::new();
    let mut tables = Vec::new();
    let mut per_layer: BTreeMap<u32, usize> = BTreeMap::new();
    let mut fused: BTreeMap<u32, usize> = BTreeMap::new();
    let mut summary = Summary::default();
    for name in ckpt.names() {
        match adapter.classify(name) {
            Class::Dense => dense.push(name),
            Class::Table => tables.push(name),
            Class::Expert { layer } => *per_layer.entry(layer).or_default() += 1,
            Class::FusedExperts { layer } => *fused.entry(layer).or_default() += 1,
            Class::Skip => summary.skipped += 1,
        }
    }
    dense.sort_by(|a, b| natural_cmp(a, b));
    tables.sort_by(|a, b| natural_cmp(a, b));

    // Every expert tensor must belong to a complete (layer, expert) set.
    let want = adapter.num_experts() as usize * adapter.expert_tensor_count();
    for (&layer, &n) in &per_layer {
        if n != want {
            bail!("layer {layer}: {n} routed-expert tensors, expected {want}");
        }
    }
    if per_layer.len() as u32 != adapter.num_expert_layers() {
        bail!(
            "{} expert layers in checkpoint, config says {}",
            per_layer.len(),
            adapter.num_expert_layers()
        );
    }

    let source = Source {
        origin: opts.origin.clone().unwrap_or_else(|| default_origin(src)),
        model_type: adapter.model_type().to_owned(),
        fingerprint: checksum(&index_raw),
    };
    let mut writer = Writer::create(out, source)?;

    for (i, name) in dense.iter().enumerate() {
        let t = ckpt.get(name)?;
        writer.add_tensor(TensorFile::Dense, name, t.dtype, t.shape, t.bytes)?;
        if i % 200 == 0 {
            log(&format!("dense {}/{}", i + 1, dense.len()));
        }
    }
    summary.dense = dense.len();

    for (&layer, &n) in &per_layer {
        if opts
            .expert_layers
            .as_ref()
            .is_some_and(|r| !r.contains(&layer))
        {
            summary.excluded += n;
            continue;
        }
        let schema = adapter.expert_schema(&ckpt, layer)?;
        log(&format!(
            "experts layer {layer} ({} records x {} bytes)",
            adapter.num_experts(),
            schema.stride
        ));
        let fill_schema = schema.clone();
        writer.add_expert_group(layer, adapter.num_experts(), schema, |expert, record| {
            adapter
                .fill_record(&ckpt, &fill_schema, layer, expert, record)
                .map_err(|e| oominf_format::Error::Usage(format!("{e:#}")))
        })?;
        summary.expert_layers += 1;
    }

    for (&layer, &n) in &fused {
        if opts
            .expert_layers
            .as_ref()
            .is_some_and(|r| !r.contains(&layer))
        {
            summary.excluded += n;
            continue;
        }
        let schema = adapter.fused_expert_schema(&ckpt, layer)?;
        log(&format!(
            "stacked experts layer {layer} ({} records x {} bytes)",
            adapter.num_experts(),
            schema.stride
        ));
        let fill_schema = schema.clone();
        writer.add_expert_group(layer, adapter.num_experts(), schema, |expert, record| {
            adapter
                .fill_fused_record(&ckpt, &fill_schema, layer, expert, record)
                .map_err(|e| oominf_format::Error::Usage(format!("{e:#}")))
        })?;
        summary.expert_layers += 1;
    }

    if opts.tables {
        for (i, name) in tables.iter().enumerate() {
            let t = ckpt.get(name)?;
            writer.add_tensor(TensorFile::Tables, name, t.dtype, t.shape, t.bytes)?;
            log(&format!("table {}/{} {name}", i + 1, tables.len()));
        }
        summary.tables = tables.len();
    } else {
        summary.excluded += tables.len();
    }

    for file in METADATA_FILES {
        let from = src.join(file);
        if from.exists() {
            std::fs::copy(&from, out.join(file)).with_context(|| format!("copy {file}"))?;
        }
    }
    let index = writer.finish()?;
    Ok((index, summary))
}

/// The source directory's name, plus `@<revision>` when it was downloaded with
/// the Hugging Face CLI (which records the commit of each file it fetched).
pub fn default_origin(src: &Path) -> String {
    let name = src
        .canonicalize()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_default();
    let meta = src.join(".cache/huggingface/download/config.json.metadata");
    match std::fs::read_to_string(meta) {
        Ok(m) => match m.lines().next().filter(|r| !r.is_empty()) {
            Some(rev) => format!("{name}@{rev}"),
            None => name,
        },
        Err(_) => name,
    }
}

/// Orders names with embedded numbers numerically (`layers.2` before `layers.10`).
pub fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let (mut a, mut b) = (a.as_bytes(), b.as_bytes());
    loop {
        match (a.first(), b.first()) {
            (None, None) => return std::cmp::Ordering::Equal,
            (None, _) => return std::cmp::Ordering::Less,
            (_, None) => return std::cmp::Ordering::Greater,
            (Some(x), Some(y)) if x.is_ascii_digit() && y.is_ascii_digit() => {
                let na = a.iter().take_while(|c| c.is_ascii_digit()).count();
                let nb = b.iter().take_while(|c| c.is_ascii_digit()).count();
                let (da, db) = (&a[..na], &b[..nb]);
                let (ta, tb) = (strip_zeros(da), strip_zeros(db));
                let ord = ta.len().cmp(&tb.len()).then(ta.cmp(tb)).then(na.cmp(&nb));
                if ord.is_ne() {
                    return ord;
                }
                a = &a[na..];
                b = &b[nb..];
            }
            (Some(x), Some(y)) => {
                if x != y {
                    return x.cmp(y);
                }
                a = &a[1..];
                b = &b[1..];
            }
        }
    }
}

fn strip_zeros(d: &[u8]) -> &[u8] {
    let n = d.iter().take_while(|&&c| c == b'0').count();
    &d[n.min(d.len().saturating_sub(1))..]
}

#[cfg(test)]
mod tests;
