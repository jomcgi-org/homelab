//! The engine thread: owns the model and the cached sequence, and runs one
//! generation at a time.

use std::sync::mpsc as std_mpsc;
use std::time::Instant;

use anyhow::{Context, Result};
use oominf_core::{Model, Session, decode_step};
use tokio::sync::mpsc;

use crate::sampling::{Sampler, SamplingParams};
use crate::store::{PrefixStore, StoreConfig};

/// Loads the model; runs on the engine thread, which then owns it.
pub type ModelLoader = Box<dyn FnOnce() -> Result<Box<dyn Model>> + Send>;

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
    /// Removes and returns a cached sequence whose tokens prefix `prompt`;
    /// `new_session` makes a fresh session for a cache that restores saved state.
    fn take(
        &mut self,
        prompt: &[u32],
        new_session: &mut dyn FnMut() -> Result<S>,
    ) -> Option<CachedSeq<S>>;
    fn put(&mut self, seq: CachedSeq<S>);
    /// Releases every cached sequence (and the device memory it holds); a
    /// persistent cache may save it first.
    fn clear(&mut self);
    /// Releases every cached sequence without saving it (its state may be
    /// half-updated after a failed request).
    fn discard(&mut self) {
        self.clear();
    }
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

impl<S> LastSequence<S> {
    /// Removes the cached sequence, whatever it holds.
    pub fn take_all(&mut self) -> Option<CachedSeq<S>> {
        self.0.take()
    }
}

impl<S> PrefixCache<S> for LastSequence<S> {
    fn take(&mut self, prompt: &[u32], _: &mut dyn FnMut() -> Result<S>) -> Option<CachedSeq<S>> {
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

/// Starts the engine thread for sequences of up to `max_context` tokens, drafting
/// up to `draft` tokens per decode step when the model can (speculative decoding;
/// 0 disables it). `ready` receives the startup summary once the model is loaded,
/// or the load error.
pub fn start(
    loader: ModelLoader,
    max_context: usize,
    draft: usize,
    store: Option<StoreConfig>,
) -> (EngineHandle, std_mpsc::Receiver<Result<String>>) {
    let (jobs_tx, jobs_rx) = std_mpsc::channel::<Job>();
    let (ready_tx, ready_rx) = std_mpsc::channel();
    std::thread::Builder::new()
        .name("oominf-engine".into())
        .spawn(move || {
            let engine = match Engine::load(loader, max_context, draft, store) {
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

type Seq = Box<dyn Session>;

/// Caches `seq`; the session must hold exactly its tokens.
fn keep(cache: &mut dyn PrefixCache<Seq>, seq: CachedSeq<Seq>) {
    debug_assert_eq!(
        seq.state.len(),
        seq.ids.len(),
        "session and token list disagree"
    );
    cache.put(seq);
}

struct Engine {
    model: Box<dyn Model>,
    cache: Box<dyn PrefixCache<Seq>>,
    max_context: usize,
    /// Draft tokens per decode step (0: no speculative decoding).
    draft: usize,
}

impl Engine {
    fn load(
        loader: ModelLoader,
        max_context: usize,
        draft: usize,
        store: Option<StoreConfig>,
    ) -> Result<(Self, String)> {
        let t = Instant::now();
        let model = loader()?;
        let (cache, stored): (Box<dyn PrefixCache<Seq>>, String) = match store {
            Some(cfg) => {
                let dir = cfg.dir.display().to_string();
                (
                    Box::new(PrefixStore::open(cfg)?),
                    format!("; prefix store {dir}"),
                )
            }
            None => (Box::new(LastSequence::default()), String::new()),
        };
        let summary = format!(
            "model loaded in {:.1}s; {}; max context {max_context} tokens; draft {draft} tokens per step{stored}",
            t.elapsed().as_secs_f64(),
            model.describe(),
        );
        Ok((
            Engine {
                model,
                cache,
                max_context,
                draft,
            },
            summary,
        ))
    }

    fn run(mut self, jobs: std_mpsc::Receiver<Job>) {
        while let Ok(job) = jobs.recv() {
            let events = job.events.clone();
            if let Err(e) = self.serve(job) {
                // The cached state may be half-updated: drop it unsaved.
                self.cache.discard();
                let _ = events.blocking_send(Event::Failed(format!("{e:#}")));
            }
        }
    }

    fn serve(&mut self, job: Job) -> Result<()> {
        let max_context = self.max_context;
        anyhow::ensure!(!job.prompt.is_empty(), "empty prompt");
        anyhow::ensure!(
            job.prompt.len() < max_context,
            "prompt is {} tokens; the context limit is {max_context}",
            job.prompt.len()
        );
        let max_tokens = job.max_tokens.min(max_context - job.prompt.len());

        let model = &self.model;
        let restored = self
            .cache
            .take(&job.prompt, &mut || model.new_session(max_context));
        let (mut ids, mut state, cached_logits) = match restored {
            Some(c) => (c.ids, c.state, Some(c.logits)),
            None => {
                // Free the stale sequence before starting its replacement.
                self.cache.clear();
                (Vec::new(), self.model.new_session(max_context)?, None)
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
            keep(
                &mut *self.cache,
                CachedSeq {
                    ids,
                    state,
                    logits: cached_logits.unwrap_or_default(),
                },
            );
            return Ok(());
        }

        let fresh = &job.prompt[cached..];
        let mut logits = if fresh.is_empty() {
            cached_logits.context("cached sequence has no logits")?
        } else {
            match state.prefill(fresh, &|| job.events.is_closed())? {
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
        let (mut emitted, mut drafted, mut accepted) = (0, 0, 0);
        // `next` was chosen from `logits` (after the last token of `ids`) and is not
        // fed yet.
        let mut next = sampler.sample(&logits);
        'generate: loop {
            if job.stop_ids.contains(&next) {
                finish = FinishReason::Stop;
                break;
            }
            if job.events.blocking_send(Event::Token(next)).is_err() {
                keep(&mut *self.cache, CachedSeq { ids, state, logits });
                return Ok(());
            }
            emitted += 1;
            if emitted == max_tokens {
                break;
            }
            let d = decode_step(
                &mut *state,
                next,
                self.draft,
                |row: &[f32], draft: Option<u32>| Ok(sampler.choose(row, draft)),
            )?;
            drafted += d.drafted;
            accepted += d.accepted;
            ids.push(next);
            // `tokens[..n - 1]` are accepted drafts, already fed; `tokens[n - 1]` is
            // the new `next`. A stop, a closed client or the token limit inside the
            // step cuts the sequence back to what was emitted.
            let n = d.tokens.len();
            for (i, &tok) in d.tokens.iter().enumerate() {
                logits.clone_from(&d.rows[i]);
                if i + 1 == n {
                    next = tok;
                    continue 'generate;
                }
                if job.stop_ids.contains(&tok) {
                    state.rewind(n - 1 - i)?;
                    finish = FinishReason::Stop;
                    break 'generate;
                }
                if job.events.blocking_send(Event::Token(tok)).is_err() {
                    state.rewind(n - 1 - i)?;
                    keep(&mut *self.cache, CachedSeq { ids, state, logits });
                    return Ok(());
                }
                ids.push(tok);
                emitted += 1;
                if emitted == max_tokens {
                    state.rewind(n - 2 - i)?;
                    logits.clone_from(&d.rows[i + 1]);
                    break 'generate;
                }
            }
        }
        if drafted > 0 {
            eprintln!(
                "oominf: {emitted} tokens, drafts accepted {accepted}/{drafted} ({:.0}%)",
                100.0 * accepted as f64 / drafted as f64
            );
        }
        keep(&mut *self.cache, CachedSeq { ids, state, logits });
        let _ = job.events.blocking_send(Event::Finished(finish));
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn no_session() -> Result<()> {
        anyhow::bail!("a last-sequence cache never makes sessions")
    }

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
        assert!(c.take(&[1, 9, 3, 4], &mut no_session).is_none());
        assert!(c.take(&[1, 2], &mut no_session).is_none());
        assert_eq!(
            c.take(&[1, 2, 3, 4], &mut no_session).map(|s| s.ids),
            Some(vec![1, 2, 3])
        );
        assert!(
            c.take(&[1, 2, 3, 4], &mut no_session).is_none(),
            "taking empties the cache"
        );
    }

    #[test]
    fn identical_prompt_needs_cached_logits() {
        let mut c = LastSequence::default();
        c.put(seq(&[1, 2], &[]));
        assert!(c.take(&[1, 2], &mut no_session).is_none());
        c.put(seq(&[1, 2], &[0.1, 0.2]));
        assert!(c.take(&[1, 2], &mut no_session).is_some());
    }

    /// A model over 8 tokens whose logits after `x` peak at `(x + 1) % 8`, and
    /// whose drafts always follow that rule (so every draft is accepted).
    struct Counting;

    struct CountingSession {
        fed: Vec<u32>,
        /// Tokens fed by the last `step_all` (what `rewind` may drop).
        last_step: usize,
    }

    fn row(x: u32) -> Vec<f32> {
        let mut r = vec![0.0; 8];
        r[((x + 1) % 8) as usize] = 1.0;
        r
    }

    impl Model for Counting {
        fn model_type(&self) -> &str {
            "counting"
        }
        fn vocab(&self) -> usize {
            8
        }
        fn new_session(&self, _max_tokens: usize) -> Result<Box<dyn Session>> {
            Ok(Box::new(CountingSession {
                fed: Vec::new(),
                last_step: 0,
            }))
        }
        fn describe(&self) -> String {
            "counting".into()
        }
        fn expert_stats(&self) -> oominf_core::ExpertStats {
            Default::default()
        }
    }

    impl Session for CountingSession {
        fn len(&self) -> usize {
            self.fed.len()
        }
        fn prefill(&mut self, tokens: &[u32], _: &dyn Fn() -> bool) -> Result<Option<Vec<f32>>> {
            self.fed.extend(tokens);
            Ok(Some(row(*tokens.last().unwrap())))
        }
        fn step(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
            self.fed.extend(tokens);
            self.last_step = 0;
            Ok(row(*tokens.last().unwrap()))
        }
        fn step_all(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
            self.fed.extend(tokens);
            self.last_step = tokens.len();
            Ok(tokens.iter().flat_map(|&t| row(t)).collect())
        }
        fn rewind(&mut self, n: usize) -> Result<()> {
            anyhow::ensure!(
                n < self.last_step.max(1),
                "rewind {n} of {}",
                self.last_step
            );
            self.fed.truncate(self.fed.len() - n);
            self.last_step -= n;
            Ok(())
        }
        fn draft(&mut self, next: u32, k: usize) -> Result<Vec<u32>> {
            Ok((1..=k as u32).map(|i| (next + i) % 8).collect())
        }
    }

    /// Runs one job and returns the emitted tokens and the finish reason.
    fn run(
        handle: &EngineHandle,
        prompt: &[u32],
        max_tokens: usize,
        stop: &[u32],
    ) -> (usize, Vec<u32>, Option<FinishReason>) {
        let (tx, mut rx) = mpsc::channel(64);
        handle
            .submit(Job {
                prompt: prompt.to_vec(),
                sampling: SamplingParams {
                    temperature: 0.0,
                    ..Default::default()
                },
                max_tokens,
                stop_ids: stop.to_vec(),
                events: tx,
            })
            .unwrap();
        let (mut cached, mut tokens, mut finish) = (0, Vec::new(), None);
        while let Some(e) = rx.blocking_recv() {
            match e {
                Event::Started { cached: c, .. } => cached = c,
                Event::Token(t) => tokens.push(t),
                Event::Finished(f) => finish = Some(f),
                Event::Failed(m) => panic!("{m}"),
            }
        }
        (cached, tokens, finish)
    }

    fn engine(draft: usize) -> EngineHandle {
        let (handle, ready) = start(Box::new(|| Ok(Box::new(Counting))), 1024, draft, None);
        ready.recv().unwrap().unwrap();
        handle
    }

    #[test]
    fn speculative_steps_emit_what_one_token_steps_emit() {
        for draft in [0, 1, 3] {
            let h = engine(draft);
            let (_, tokens, finish) = run(&h, &[0], 10, &[]);
            assert_eq!(tokens, [1, 2, 3, 4, 5, 6, 7, 0, 1, 2], "draft {draft}");
            assert_eq!(finish, Some(FinishReason::Length));
        }
    }

    #[test]
    fn stops_and_limits_inside_a_step_leave_exactly_the_emitted_tokens() {
        // With 3 drafts a step emits 4 tokens: the stop token and the token limit
        // both land inside a step, and the cached sequence must hold exactly the
        // prompt and the emitted tokens (the next request extends it).
        let h = engine(3);
        let (_, tokens, finish) = run(&h, &[0], 100, &[6]);
        assert_eq!(tokens, [1, 2, 3, 4, 5]);
        assert_eq!(finish, Some(FinishReason::Stop));
        let mut prompt = vec![0, 1, 2, 3, 4, 5];
        prompt.push(6);
        let (cached, tokens, _) = run(&h, &prompt, 6, &[]);
        assert_eq!(cached, 6, "reuses the prompt and every emitted token");
        assert_eq!(tokens, [7, 0, 1, 2, 3, 4]);
        // The limit landed on an accepted draft, already fed: it stays, the drafts
        // after it are rewound.
        prompt.extend([7, 0, 1, 2, 3, 4, 5]);
        let (cached, _, _) = run(&h, &prompt, 1, &[]);
        assert_eq!(cached, 13, "the prompt and every emitted token");
    }
}
