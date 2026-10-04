//! Qwen 3.8 Flash (`qwen4_exp`) on the oominf CUDA backend.
//!
//! The residual stream is `[T, hc * hidden]` fp32. Each decoder layer mixes the
//! `hc` streams down to one block input, runs the token mixer (Gated DeltaNet on
//! linear-attention layers), combines back, then does the same around the MoE.
//! Dense GEMMs use bf16 operands (the released weight precision) with fp32
//! accumulation; routed experts are dequantised from NVFP4 and run in fp32
//! (W4A16 with no extra rounding).

mod linear_layer;

pub use linear_layer::{GdnState, LinearLayer};

/// Model dimensions, from `config.json` `text_config`.
#[derive(Debug, Clone)]
pub struct Dims {
    pub hidden: usize,
    pub hc: usize,
    pub hc_lowrank: usize,
    pub k_heads: usize,
    pub v_heads: usize,
    pub head_k: usize,
    pub head_v: usize,
    pub conv_kernel: usize,
    pub experts: usize,
    pub top_k: usize,
    pub moe_inter: usize,
    pub shared_inter: usize,
    pub eps: f32,
}

impl Dims {
    pub fn from_config(config: &str) -> anyhow::Result<Self> {
        let v: serde_json::Value = serde_json::from_str(config)?;
        let t = v.get("text_config").unwrap_or(&v);
        let u = |k: &str| -> anyhow::Result<usize> {
            t[k].as_u64()
                .map(|x| x as usize)
                .ok_or_else(|| anyhow::anyhow!("config text_config.{k}"))
        };
        if t["output_gate_type"].as_str() != Some("sigmoid") {
            anyhow::bail!("only output_gate_type = sigmoid is implemented");
        }
        Ok(Dims {
            hidden: u("hidden_size")?,
            hc: u("hc_count")?,
            hc_lowrank: u("hc_lowrank")?,
            k_heads: u("linear_num_key_heads")?,
            v_heads: u("linear_num_value_heads")?,
            head_k: u("linear_key_head_dim")?,
            head_v: u("linear_value_head_dim")?,
            conv_kernel: u("linear_conv_kernel_dim")?,
            experts: u("num_experts")?,
            top_k: u("num_experts_per_tok")?,
            moe_inter: u("moe_intermediate_size")?,
            shared_inter: u("shared_expert_intermediate_size")?,
            eps: t["rms_norm_eps"].as_f64().unwrap_or(1e-6) as f32,
        })
    }

    pub fn key_dim(&self) -> usize {
        self.k_heads * self.head_k
    }

    pub fn value_dim(&self) -> usize {
        self.v_heads * self.head_v
    }

    pub fn conv_dim(&self) -> usize {
        2 * self.key_dim() + self.value_dim()
    }

    pub fn residual(&self) -> usize {
        self.hc * self.hidden
    }
}

/// Observes and optionally substitutes named stage tensors (fp32, row-major).
///
/// `observe` receives every stage the layer computes; returning `Some` from
/// `substitute` replaces that stage's value before the layer continues, which lets
/// a harness test each stage on exact reference inputs.
pub trait Probe {
    fn wants(&self, _stage: &str) -> bool {
        true
    }
    fn observe(&mut self, stage: &str, data: Vec<f32>);
    fn substitute(&mut self, _stage: &str) -> Option<Vec<f32>> {
        None
    }
}

/// A probe that does nothing.
pub struct NoProbe;

impl Probe for NoProbe {
    fn wants(&self, _stage: &str) -> bool {
        false
    }
    fn observe(&mut self, _stage: &str, _data: Vec<f32>) {}
}
