//! Qwen 3.8 Flash behind the model-neutral [`Model`] and [`Session`] interfaces.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

use anyhow::{Context, Result};
use oominf_core::{Backend, ExpertFactory, ExpertSource, ExpertStats, Model, NoProbe, Session};
use oominf_format::Model as Files;

use crate::{Dims, QwenModel, SeqState};

pub const MODEL_TYPE: &str = "qwen4_exp";

/// How a loaded model runs its sequences.
#[derive(Debug, Clone)]
pub struct Options {
    /// Longest sequence (prompt plus generation) a session is sized for by default.
    pub max_context: usize,
    /// Prompt tokens per prefill chunk (bounds prefill workspace memory).
    pub prefill_chunk: usize,
    /// CPU threads computing host-resident experts of decode-sized steps (0: copy
    /// every routed record to the device).
    pub host_threads: usize,
    /// How attention layers store their KV cache.
    pub kv: oominf_core::KvFormat,
    /// How dense weights are stored.
    pub dense: oominf_core::DenseFormat,
    /// Activation precision of the prefill expert GEMM.
    pub expert_precision: oominf_core::ExpertPrecision,
    /// Attention K/V caches in host memory rather than device memory.
    pub kv_host: bool,
}

struct Inner<B: Backend> {
    b: Arc<B>,
    model: QwenModel<B>,
    experts: RefCell<Box<dyn ExpertSource<B>>>,
    /// A session state allocated before the expert source, handed to the first
    /// session of `max_context` tokens.
    spare: RefCell<Option<SeqState<B>>>,
    opts: Options,
}

struct Qwen<B: Backend>(Rc<Inner<B>>);

struct QwenSession<B: Backend> {
    inner: Rc<Inner<B>>,
    state: SeqState<B>,
}

/// Loads the model from a converted checkpoint onto `b`.
pub fn open<B: Backend>(
    b: Arc<B>,
    files: Arc<Files>,
    opts: Options,
    experts: ExpertFactory<B>,
) -> Result<Box<dyn Model>> {
    let config = std::fs::read_to_string(files.dir().join("config.json")).context("config.json")?;
    let mut dims = Dims::from_config(&config)?;
    dims.kv = opts.kv;
    dims.dense = opts.dense;
    dims.expert_precision = opts.expert_precision;
    dims.kv_host = opts.kv_host;
    let mut model = QwenModel::load(&*b, &files, dims, None)?;
    if opts.host_threads > 0 {
        model.set_host_experts(Arc::new(oominf_cpu::HostExperts::new(opts.host_threads)?));
    }
    let spare = model.new_state(&*b, opts.max_context)?;
    b.sync()?;
    let experts = experts(&b)?;
    b.sync()?;
    Ok(Box::new(Qwen(Rc::new(Inner {
        b,
        model,
        experts: RefCell::new(experts),
        spare: RefCell::new(Some(spare)),
        opts,
    }))))
}

impl<B: Backend> Model for Qwen<B> {
    fn model_type(&self) -> &str {
        MODEL_TYPE
    }

    fn vocab(&self) -> usize {
        self.0.model.vocab()
    }

    fn new_session(&self, max_tokens: usize) -> Result<Box<dyn Session>> {
        let inner = &self.0;
        // Let frees of a dropped session land before allocating its replacement.
        inner.b.sync()?;
        let spare = inner
            .spare
            .borrow_mut()
            .take_if(|_| max_tokens == inner.opts.max_context);
        let state = match spare {
            Some(s) => s,
            None => inner.model.new_state(&*inner.b, max_tokens)?,
        };
        // A dropped sequence may have grown its caches at the expert cache's
        // expense: give the memory back.
        inner
            .model
            .reclaim_vram(&*inner.b, &state, inner.experts.borrow_mut().as_mut())?;
        Ok(Box::new(QwenSession {
            inner: inner.clone(),
            state,
        }))
    }

    fn describe(&self) -> String {
        format!(
            "{MODEL_TYPE}, {} layers; {}",
            self.0.model.dims.layer_kinds.len(),
            self.0.experts.borrow().describe()
        )
    }

    fn expert_stats(&self) -> ExpertStats {
        self.0.experts.borrow().stats()
    }

    fn expert_tiers(&self) -> oominf_core::ExpertTiers {
        self.0.experts.borrow().tiers()
    }
}

impl<B: Backend> Session for QwenSession<B> {
    fn len(&self) -> usize {
        self.state.pos
    }

    fn plan_checkpoints(&mut self, positions: &[usize]) {
        self.state.plan = positions.to_vec();
    }

    fn checkpoints(&self) -> Vec<usize> {
        self.state.checkpoints.iter().map(|c| c.pos).collect()
    }

    fn rewind_to(&mut self, pos: usize) -> Result<()> {
        self.inner
            .model
            .rewind_to(&*self.inner.b, &mut self.state, pos)
    }

    fn save(&self, w: &mut dyn std::io::Write) -> Result<()> {
        crate::snapshot::save(&self.inner.model, &*self.inner.b, &self.state, w)
    }

    fn load(&mut self, r: &mut dyn std::io::Read) -> Result<()> {
        let inner = &self.inner;
        crate::snapshot::load(
            &inner.model,
            &*inner.b,
            &mut self.state,
            r,
            inner.experts.borrow_mut().as_mut(),
        )
    }

    fn prefill(
        &mut self,
        tokens: &[u32],
        cancelled: &dyn Fn() -> bool,
    ) -> Result<Option<Vec<f32>>> {
        let inner = &self.inner;
        let out = inner.model.prefill(
            &*inner.b,
            tokens,
            inner.opts.prefill_chunk,
            &mut self.state,
            inner.experts.borrow_mut().as_mut(),
            cancelled,
        )?;
        out.map(|o| inner.b.download_f32(&o)).transpose()
    }

    fn step_all(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
        let inner = &self.inner;
        let out = inner.model.step(
            &*inner.b,
            tokens,
            &mut self.state,
            inner.experts.borrow_mut().as_mut(),
            &mut NoProbe,
            false,
            true,
        )?;
        inner.b.download_f32(&out)
    }

    fn rewind(&mut self, n: usize) -> Result<()> {
        self.inner.model.rewind(&*self.inner.b, &mut self.state, n)
    }

    fn draft(&mut self, next: u32, k: usize) -> Result<Vec<u32>> {
        let inner = &self.inner;
        inner.model.draft(
            &*inner.b,
            &mut self.state,
            next,
            k,
            inner.experts.borrow_mut().as_mut(),
        )
    }

    fn step(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
        let inner = &self.inner;
        let out = inner.model.forward(
            &*inner.b,
            tokens,
            &mut self.state,
            inner.experts.borrow_mut().as_mut(),
            &mut NoProbe,
            true,
        )?;
        inner.b.download_f32(&out)
    }
}
