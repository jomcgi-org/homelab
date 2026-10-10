//! Opens a converted model with the implementation for its family.
//!
//! The family comes from `model_type` in the model's `config.json`. Adding a family
//! means adding its crate and one arm to [`open`].

use std::sync::Arc;

use anyhow::{Context, Result, bail};
use oominf_core::{Backend, ExpertFactory, Model};
use oominf_format::Model as Files;

/// Model families this build can run, by `model_type`.
pub const SUPPORTED: &[&str] = &[oominf_models_qwen::MODEL_TYPE];

/// How a loaded model runs its sequences.
#[derive(Debug, Clone)]
pub struct Options {
    /// Longest sequence (prompt plus generation) a session is sized for by default.
    pub max_context: usize,
    /// Prompt tokens per prefill chunk; `None` uses the family's default.
    pub prefill_chunk: Option<usize>,
    /// CPU threads computing host-resident routed experts of decode-sized steps (0:
    /// every routed record is copied to the device).
    pub host_threads: usize,
    /// How attention layers store their KV cache.
    pub kv: oominf_core::KvFormat,
    /// How dense weights are stored.
    pub dense: oominf_core::DenseFormat,
    /// Activation precision of the prefill expert GEMM.
    pub expert_precision: oominf_core::ExpertPrecision,
    /// Arithmetic of prefill attention.
    pub attention_precision: oominf_core::AttentionPrecision,
    /// Attention K/V caches in host memory rather than device memory.
    pub kv_host: bool,
}

/// The `model_type` of a converted model directory.
pub fn model_type(files: &Files) -> Result<String> {
    let path = files.dir().join("config.json");
    let config: serde_json::Value = serde_json::from_slice(
        &std::fs::read(&path).with_context(|| format!("read {}", path.display()))?,
    )?;
    config["model_type"]
        .as_str()
        .map(str::to_owned)
        .context("config.json has no model_type")
}

/// Host memory a sequence of the model in `files` takes beside the expert tiers at
/// `opts.max_context` (see [`oominf_core::HostDemand`]), without loading it.
pub fn host_demand(files: &Files, opts: &Options) -> Result<oominf_core::HostDemand> {
    match model_type(files)?.as_str() {
        oominf_models_qwen::MODEL_TYPE => {
            let config = std::fs::read_to_string(files.dir().join("config.json"))?;
            let mut d = oominf_models_qwen::Dims::from_config(&config)?;
            d.kv = opts.kv;
            d.kv_host = opts.kv_host;
            oominf_models_qwen::host_demand(&d, opts.max_context)
        }
        other => bail!("no implementation for model_type {other:?} (supported: {SUPPORTED:?})"),
    }
}

/// Loads the model in `files` onto `backend`; `experts` builds its expert source.
pub fn open<B: Backend>(
    backend: Arc<B>,
    files: Arc<Files>,
    opts: &Options,
    experts: ExpertFactory<B>,
) -> Result<Box<dyn Model>> {
    match model_type(&files)?.as_str() {
        oominf_models_qwen::MODEL_TYPE => oominf_models_qwen::open(
            backend,
            files,
            oominf_models_qwen::Options {
                max_context: opts.max_context,
                prefill_chunk: opts
                    .prefill_chunk
                    .unwrap_or(oominf_models_qwen::PREFILL_CHUNK),
                host_threads: opts.host_threads,
                kv: opts.kv,
                dense: opts.dense,
                expert_precision: opts.expert_precision,
                attention_precision: opts.attention_precision,
                kv_host: opts.kv_host,
            },
            experts,
        ),
        other => bail!("no implementation for model_type {other:?} (supported: {SUPPORTED:?})"),
    }
}
