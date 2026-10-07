//! Text-only Qwen 3.5 MoE in ModelOpt NVFP4. Its routed expert records use the
//! same three projections and group-16 scales as Qwen 3.8 Flash.
use super::{Adapter, Class, qwen38::Qwen38};
use crate::safetensors::Checkpoint;
use anyhow::Result;
use oominf_format::RecordSchema;
use serde_json::Value;

pub struct Qwen35 {
    records: Qwen38,
    model_type: String,
}

impl Qwen35 {
    pub fn from_config(config: &Value) -> Result<Self> {
        Ok(Self {
            records: Qwen38::from_config(config)?,
            model_type: config["model_type"].as_str().unwrap_or_default().to_owned(),
        })
    }
}

impl Adapter for Qwen35 {
    fn model_type(&self) -> &str {
        &self.model_type
    }
    fn num_expert_layers(&self) -> u32 {
        self.records.num_expert_layers()
    }
    fn num_experts(&self) -> u32 {
        self.records.num_experts()
    }
    fn classify(&self, name: &str) -> Class {
        // Text inference uses the main decoder; vision and the optional draft head
        // are excluded explicitly rather than retained in the resident weights.
        if name.starts_with("mtp.") || name.starts_with("model.visual.") {
            Class::Skip
        } else {
            self.records.classify(name)
        }
    }
    fn expert_schema(&self, ckpt: &Checkpoint, layer: u32) -> Result<RecordSchema> {
        self.records.expert_schema(ckpt, layer)
    }
    fn fill_record(
        &self,
        ckpt: &Checkpoint,
        schema: &RecordSchema,
        layer: u32,
        expert: u32,
        record: &mut [u8],
    ) -> Result<()> {
        self.records
            .fill_record(ckpt, schema, layer, expert, record)
    }
    fn expert_tensor_count(&self) -> usize {
        self.records.expert_tensor_count()
    }
}
