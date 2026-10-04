//! Per-layer embedding (PLE): hashed n-gram embeddings gathered from large FP8
//! tables and added to the residual before a layer's attention hyper-connection.
//!
//! Stub: the interface is fixed so layers, state and the harness can be wired;
//! the implementation lands separately.

use anyhow::{Result, bail};
use oominf_cuda::{Bf16Buf, Buf, Gpu};
use oominf_format::Model;

use crate::layer::StepInput;
use crate::{Dims, Probe};

pub struct Ple {}

/// PLE short-conv state and n-gram token context carried across steps.
pub struct PleState {}

impl Ple {
    pub fn load(_gpu: &Gpu, _model: &Model, _d: &Dims, layer: u32) -> Result<Self> {
        bail!("layer {layer}: PLE is not implemented yet")
    }

    pub fn new_state(&self, _gpu: &Gpu, _d: &Dims) -> Result<PleState> {
        Ok(PleState {})
    }

    /// Returns the PLE contribution `[t, hc * hidden]` to add to `residual`.
    #[allow(clippy::too_many_arguments)]
    pub fn forward(
        &self,
        _gpu: &Gpu,
        _d: &Dims,
        _residual: &Buf,
        _t: usize,
        _step: &StepInput,
        _state: &mut PleState,
        _scratch: &mut Bf16Buf,
        _probe: &mut dyn Probe,
    ) -> Result<Buf> {
        bail!("PLE is not implemented yet")
    }
}
