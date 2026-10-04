//! Model adapters map an upstream checkpoint's tensor names onto the format:
//! which tensors are routed-expert record parts, which are gather tables, which
//! are dense, and which are skipped.

pub mod qwen38;

use anyhow::{Result, bail};
use oominf_format::RecordSchema;
use serde_json::Value;

use crate::safetensors::Checkpoint;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Dense,
    Table,
    Expert { layer: u32 },
    Skip,
}

pub trait Adapter {
    fn model_type(&self) -> &str;
    fn num_expert_layers(&self) -> u32;
    fn num_experts(&self) -> u32;
    fn classify(&self, name: &str) -> Class;
    /// Record schema for one layer's routed experts.
    fn expert_schema(&self, ckpt: &Checkpoint, layer: u32) -> Result<RecordSchema>;
    /// Copies one expert's tensors into a zeroed record buffer.
    fn fill_record(
        &self,
        ckpt: &Checkpoint,
        schema: &RecordSchema,
        layer: u32,
        expert: u32,
        record: &mut [u8],
    ) -> Result<()>;
    /// Source tensors per expert, used to check every one was consumed.
    fn expert_tensor_count(&self) -> usize;
}

pub fn for_config(config: &Value) -> Result<Box<dyn Adapter>> {
    let model_type = config["model_type"].as_str().unwrap_or_default();
    match model_type {
        "qwen4_exp" => Ok(Box::new(qwen38::Qwen38::from_config(config)?)),
        other => bail!("no converter adapter for model_type {other:?}"),
    }
}
