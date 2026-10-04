//! Full attention with the QSA sparse indexer (every 4th layer of Qwen 3.8 Flash).
//!
//! Stub: the interface is fixed so layers, state and the harness can be wired;
//! the implementation lands separately.

use anyhow::{Result, bail};
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::layer::StepInput;
use crate::{Dims, Probe};

pub struct Attention {}

/// KV cache (and indexer cache) for one attention layer.
pub struct AttnState {}

impl Attention {
    pub fn load(_gpu: &Gpu, _model: &Model, _d: &Dims, layer: u32) -> Result<Self> {
        bail!("layer {layer}: full attention is not implemented yet")
    }

    pub fn new_state(&self, _gpu: &Gpu, _d: &Dims, _max_tokens: usize) -> Result<AttnState> {
        Ok(AttnState {})
    }

    /// `x` is the block input `[t, hidden]`; returns the mixer output `[t, hidden]`.
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        _gpu: &Gpu,
        _d: &Dims,
        _x: &Buf,
        _t: usize,
        _step: &StepInput,
        _state: &mut AttnState,
        _scratch: &mut Bf16Buf,
        _probe: &mut dyn Probe,
    ) -> Result<Buf> {
        bail!("full attention is not implemented yet")
    }
}
