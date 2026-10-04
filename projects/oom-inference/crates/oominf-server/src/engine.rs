//! The engine thread: owns the GPU, the model, the expert source and the cached
//! sequence, and runs one generation at a time.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::mpsc as std_mpsc;
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_cuda::Gpu;
use oominf_format::Model;
use oominf_models_qwen::{Dims, ExpertSource, NoProbe, QwenModel, SeqState};
use tokio::sync::mpsc;

use crate::sampling::{Sampler, SamplingParams};

/// Builds the expert source on the engine thread; returns it with a description
/// for the startup log.
pub type ExpertFactory =
    Box<dyn FnOnce(&Gpu, &Arc<Model>) -> Result<(Box<dyn ExpertSource>, String)> + Send>;

#[derive(Debug, Clone)]
pub struct EngineConfig {
    pub model_dir: PathBuf,
    /// Longest sequence (prompt plus generation) the KV cache is sized for.
    pub max_context: usize,
    /// Prompt tokens per prefill forward pass.
    pub prefill_chunk: usize,
}

pub struct Job {
    pub prompt: Vec<u32>,
    pub sampling: SamplingParams,
    pub max_tokens: usize,
    /// Token ids that end generation (not emitted).
    pub stop_ids: Vec<u32>,
    pub events: mpsc::Sender<Event>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FinishReason {
    /// A stop token was generated.
    Stop,
    /// `max_tokens` (or the context limit) was reached.
    Length,
}

#[derive(Debug)]
pub enum Event {
    /// Generation accepted; `cached` prompt tokens were reused.
    Started {
        prompt_tokens: usize,
        cached: usize,
    },
    Token(u32),
    Finished(FinishReason),
    Failed(String),
}

/// Reuses model state across requests. Recurrent layers cannot be rewound, so
/// a cached state is reusable only when the new prompt extends its tokens.
pub trait PrefixCache<S> {
    /// Removes and returns a cached sequence whose tokens prefix `prompt`.
    fn take(&mut self, prompt: &[u32]) -> Option<CachedSeq<S>>;
    fn put(&mut self, seq: CachedSeq<S>);
    /// Releases every cached sequence (and the device memory it holds).
    fn clear(&mut self);
}

pub struct CachedSeq<S> {
    pub ids: Vec<u32>,
    pub state: S,
    /// Logits after the last token of `ids` (empty if unknown).
    pub logits: Vec<f32>,
}

/// Keeps only the most recent sequence (single-user chat and agent loops).
pub struct LastSequence<S>(Option<CachedSeq<S>>);

impl<S> Default for LastSequence<S> {
    fn default() -> Self {
        LastSequence(None)
    }
}

impl<S> PrefixCache<S> for LastSequence<S> {
    fn take(&mut self, prompt: &[u32]) -> Option<CachedSeq<S>> {
        // An identical prompt needs the cached logits; a longer one only the state.
        let ok = self.0.as_ref().is_some_and(|c| {
            !c.ids.is_empty()
                && prompt.starts_with(&c.ids)
                && (prompt.len() > c.ids.len() || !c.logits.is_empty())
        });
        if ok { self.0.take() } else { None }
    }

    fn put(&mut self, seq: CachedSeq<S>) {
        self.0 = Some(seq);
    }

    fn clear(&mut self) {
        self.0 = None;
    }
}

#[derive(Clone)]
pub struct EngineHandle {
    jobs: std_mpsc::Sender<Job>,
}

impl EngineHandle {
    /// A handle over any job queue (the engine thread, or a stand-in in tests).
    pub fn new(jobs: std_mpsc::Sender<Job>) -> Self {
        EngineHandle { jobs }
    }

    pub fn submit(&self, job: Job) -> Result<()> {
        self.jobs
            .send(job)
            .map_err(|_| anyhow::anyhow!("engine is not running"))
    }
}

/// Starts the engine thread. `ready` receives the startup summary once the model
/// is loaded, or the load error.
pub fn start(
    cfg: EngineConfig,
    experts: ExpertFactory,
) -> (EngineHandle, std_mpsc::Receiver<Result<String>>) {
    let (jobs_tx, jobs_rx) = std_mpsc::channel::<Job>();
    let (ready_tx, ready_rx) = std_mpsc::channel();
    std::thread::Builder::new()
        .name("oominf-engine".into())
        .spawn(move || {
            let engine = match Engine::load(&cfg, experts) {
                Ok((engine, summary)) => {
                    let _ = ready_tx.send(Ok(summary));
                    engine
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(e));
                    return;
                }
            };
            engine.run(jobs_rx);
        })
        .expect("spawn engine thread");
    (EngineHandle::new(jobs_tx), ready_rx)
}

struct Engine {
    gpu: Gpu,
    model: QwenModel,
    experts: Box<dyn ExpertSource>,
    cache: Box<dyn PrefixCache<SeqState>>,
    /// A fresh state allocated at load, so expert tiers are sized around it.
    spare: Option<SeqState>,
    cfg: EngineConfig,
}

impl Engine {
    fn load(cfg: &EngineConfig, experts: ExpertFactory) -> Result<(Self, String)> {
        let t = Instant::now();
        let files = Arc::new(Model::open(&cfg.model_dir)?);
        let dims = Dims::from_config(
            &std::fs::read_to_string(cfg.model_dir.join("config.json")).context("config.json")?,
        )?;
        let gpu = Gpu::new(0)?;
        let model = QwenModel::load(&gpu, &files, dims, None)?;
        // Allocate the sequence state first: the expert tiers take what is left.
        let spare = model.new_state(&gpu, cfg.max_context)?;
        gpu.sync()?;
        let (experts, tiers) = experts(&gpu, &files)?;
        gpu.sync()?;
        let summary = format!(
            "model loaded in {:.1}s; {tiers}; max context {} tokens",
            t.elapsed().as_secs_f64(),
            cfg.max_context
        );
        Ok((
            Engine {
                gpu,
                model,
                experts,
                cache: Box::new(LastSequence::default()),
                spare: Some(spare),
                cfg: cfg.clone(),
            },
            summary,
        ))
    }

    fn run(mut self, jobs: std_mpsc::Receiver<Job>) {
        while let Ok(job) = jobs.recv() {
            let events = job.events.clone();
            if let Err(e) = self.serve(job) {
                // The cached state may be half-updated: drop it.
                self.cache = Box::new(LastSequence::default());
                let _ = events.blocking_send(Event::Failed(format!("{e:#}")));
            }
        }
    }

    /// Prefills `ids` (layer by layer, in prefill-sized chunks) and returns the last
    /// row of logits, or `None` if the client went away.
    fn feed(
        &mut self,
        ids: &[u32],
        state: &mut SeqState,
        events: &mpsc::Sender<Event>,
    ) -> Result<Option<Vec<f32>>> {
        let out = self.model.prefill(
            &self.gpu,
            ids,
            self.cfg.prefill_chunk,
            state,
            self.experts.as_mut(),
            &|| events.is_closed(),
        )?;
        out.map(|o| self.gpu.download(&o))
            .transpose()
            .map_err(Into::into)
    }

    fn serve(&mut self, job: Job) -> Result<()> {
        let max_context = self.cfg.max_context;
        anyhow::ensure!(!job.prompt.is_empty(), "empty prompt");
        anyhow::ensure!(
            job.prompt.len() < max_context,
            "prompt is {} tokens; the context limit is {max_context}",
            job.prompt.len()
        );
        let max_tokens = job.max_tokens.min(max_context - job.prompt.len());

        let (mut ids, mut state, cached_logits) = match self.cache.take(&job.prompt) {
            Some(c) => (c.ids, c.state, Some(c.logits)),
            None => {
                // Free the stale sequence (and let the frees land) before
                // allocating its replacement.
                self.cache.clear();
                self.gpu.sync()?;
                let state = match self.spare.take() {
                    Some(s) => s,
                    None => self.model.new_state(&self.gpu, max_context)?,
                };
                (Vec::new(), state, None)
            }
        };
        let cached = ids.len();
        if job
            .events
            .blocking_send(Event::Started {
                prompt_tokens: job.prompt.len(),
                cached,
            })
            .is_err()
        {
            self.cache.put(CachedSeq {
                ids,
                state,
                logits: cached_logits.unwrap_or_default(),
            });
            return Ok(());
        }

        let fresh = &job.prompt[cached..];
        let mut logits = if fresh.is_empty() {
            cached_logits.context("cached sequence has no logits")?
        } else {
            match self.feed(fresh, &mut state, &job.events)? {
                Some(l) => {
                    ids.extend_from_slice(fresh);
                    l
                }
                // Cancelled mid-prefill: the state is partially advanced, drop it.
                None => return Ok(()),
            }
        };

        let mut sampler = Sampler::new(job.sampling);
        let mut finish = FinishReason::Length;
        for n in 0..max_tokens {
            let next = sampler.sample(&logits);
            if job.stop_ids.contains(&next) {
                finish = FinishReason::Stop;
                break;
            }
            if job.events.blocking_send(Event::Token(next)).is_err() {
                self.cache.put(CachedSeq { ids, state, logits });
                return Ok(());
            }
            if n + 1 == max_tokens {
                break;
            }
            let out = self.model.forward(
                &self.gpu,
                &[next],
                &mut state,
                self.experts.as_mut(),
                &mut NoProbe,
                true,
            )?;
            logits = self.gpu.download(&out)?;
            ids.push(next);
        }
        self.cache.put(CachedSeq { ids, state, logits });
        let _ = job.events.blocking_send(Event::Finished(finish));
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seq(ids: &[u32], logits: &[f32]) -> CachedSeq<()> {
        CachedSeq {
            ids: ids.to_vec(),
            state: (),
            logits: logits.to_vec(),
        }
    }

    #[test]
    fn last_sequence_reuses_only_exact_extensions() {
        let mut c = LastSequence::default();
        c.put(seq(&[1, 2, 3], &[0.5]));
        assert!(c.take(&[1, 9, 3, 4]).is_none());
        assert!(c.take(&[1, 2]).is_none());
        assert_eq!(c.take(&[1, 2, 3, 4]).map(|s| s.ids), Some(vec![1, 2, 3]));
        assert!(c.take(&[1, 2, 3, 4]).is_none(), "taking empties the cache");
    }

    #[test]
    fn identical_prompt_needs_cached_logits() {
        let mut c = LastSequence::default();
        c.put(seq(&[1, 2], &[]));
        assert!(c.take(&[1, 2]).is_none());
        c.put(seq(&[1, 2], &[0.1, 0.2]));
        assert!(c.take(&[1, 2]).is_some());
    }
}
