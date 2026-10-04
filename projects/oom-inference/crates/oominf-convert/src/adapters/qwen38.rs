//! Qwen 3.8 Flash (`qwen4_exp`), as released in ModelOpt NVFP4 (routed experts
//! only; everything else at source precision, PLE n-gram tables in FP8).

use anyhow::{Context, Result, bail};
use oominf_format::{RecordSchema, put_part};
use serde_json::Value;

use super::{Adapter, Class};
use crate::safetensors::Checkpoint;

pub const LAYOUT: &str = "nvfp4-modelopt-g16";

const PROJS: [&str; 3] = ["gate", "up", "down"];

pub struct Qwen38 {
    num_layers: u32,
    num_experts: u32,
}

impl Qwen38 {
    pub fn from_config(config: &Value) -> Result<Self> {
        let text = config.get("text_config").unwrap_or(config);
        let get = |k: &str| -> Result<u32> {
            Ok(text[k]
                .as_u64()
                .with_context(|| format!("config text_config.{k}"))? as u32)
        };
        Ok(Qwen38 {
            num_layers: get("num_hidden_layers")?,
            num_experts: get("num_experts")?,
        })
    }

    fn expert_name(layer: u32, expert: u32, proj: &str, field: &str) -> String {
        format!("model.language_model.layers.{layer}.mlp.experts.{expert}.{proj}_proj.{field}")
    }
}

/// Parses `model.language_model.layers.{L}.mlp.experts.{E}.<rest>`.
fn parse_expert(name: &str) -> Option<(u32, u32)> {
    let rest = name.strip_prefix("model.language_model.layers.")?;
    let (layer, rest) = rest.split_once('.')?;
    let rest = rest.strip_prefix("mlp.experts.")?;
    let (expert, _) = rest.split_once('.')?;
    Some((layer.parse().ok()?, expert.parse().ok()?))
}

impl Adapter for Qwen38 {
    fn model_type(&self) -> &str {
        "qwen4_exp"
    }

    fn num_expert_layers(&self) -> u32 {
        self.num_layers
    }

    fn num_experts(&self) -> u32 {
        self.num_experts
    }

    fn classify(&self, name: &str) -> Class {
        if name.starts_with("model.visual.") {
            return Class::Skip;
        }
        if let Some((layer, _)) = parse_expert(name) {
            return Class::Expert { layer };
        }
        if name.contains(".ple.ple_embedding.ngram_embedding.shard_") && name.ends_with(".weight") {
            return Class::Table;
        }
        Class::Dense
    }

    fn expert_schema(&self, ckpt: &Checkpoint, layer: u32) -> Result<RecordSchema> {
        let mut parts: Vec<(String, String, Vec<u64>)> =
            vec![("scalars".into(), "F32".into(), vec![6])];
        for proj in PROJS {
            for field in ["weight", "weight_scale"] {
                let t = ckpt.get(&Self::expert_name(layer, 0, proj, field))?;
                parts.push((
                    format!("{proj}.{field}"),
                    t.dtype.to_owned(),
                    t.shape.to_vec(),
                ));
            }
        }
        let borrowed: Vec<(&str, &str, &[u64])> = parts
            .iter()
            .map(|(n, d, s)| (n.as_str(), d.as_str(), s.as_slice()))
            .collect();
        Ok(RecordSchema::new(LAYOUT, &borrowed)?)
    }

    fn fill_record(
        &self,
        ckpt: &Checkpoint,
        schema: &RecordSchema,
        layer: u32,
        expert: u32,
        record: &mut [u8],
    ) -> Result<()> {
        let mut scalars = Vec::with_capacity(24);
        for proj in PROJS {
            for field in ["weight", "weight_scale"] {
                let t = ckpt.get(&Self::expert_name(layer, expert, proj, field))?;
                let part = schema
                    .part(&format!("{proj}.{field}"))
                    .context("schema part")?;
                if t.dtype != part.dtype || t.shape != part.shape.as_slice() {
                    bail!(
                        "layer {layer} expert {expert} {proj}.{field}: {} {:?}, schema expects {} {:?}",
                        t.dtype,
                        t.shape,
                        part.dtype,
                        part.shape
                    );
                }
                put_part(schema, record, &format!("{proj}.{field}"), t.bytes)?;
            }
            for field in ["weight_scale_2", "input_scale"] {
                let t = ckpt.get(&Self::expert_name(layer, expert, proj, field))?;
                if t.dtype != "F32" || t.bytes.len() != 4 {
                    bail!("layer {layer} expert {expert} {proj}.{field}: want an F32 scalar");
                }
                scalars.extend_from_slice(t.bytes);
            }
        }
        put_part(schema, record, "scalars", &scalars)?;
        Ok(())
    }

    fn expert_tensor_count(&self) -> usize {
        PROJS.len() * 4
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_names() {
        let a = Qwen38 {
            num_layers: 48,
            num_experts: 512,
        };
        assert_eq!(
            a.classify("model.language_model.layers.12.mlp.experts.511.down_proj.weight_scale"),
            Class::Expert { layer: 12 }
        );
        assert_eq!(
            a.classify("model.language_model.layers.0.mlp.shared_expert.up_proj.weight"),
            Class::Dense
        );
        assert_eq!(
            a.classify("model.language_model.layers.0.mlp.gate.weight"),
            Class::Dense
        );
        assert_eq!(
            a.classify(
                "model.language_model.layers.1.ple.ple_embedding.ngram_embedding.shard_7.weight"
            ),
            Class::Table
        );
        assert_eq!(
            a.classify(
                "model.language_model.layers.1.ple.ple_embedding.ngram_embedding.weight_scale"
            ),
            Class::Dense
        );
        assert_eq!(
            a.classify("model.visual.blocks.0.attn.qkv.weight"),
            Class::Skip
        );
        assert_eq!(
            a.classify("mtp.layers.0.mlp.experts.gate_up_proj"),
            Class::Dense
        );
    }
}
