//! The engine thread: owns the model and the cached sequence, and runs one
//! generation at a time.

use std::collections::VecDeque;
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use oominf_core::{
    ExpertStats, ExpertTiers, LOOKUP_MATCH, Model, PromptLookup, Session, decode_step_with,
};
use tokio::sync::mpsc;

use crate::sampling::{Sampler, SamplingParams};
use crate::store::{PrefixStore, StoreConfig};

mod scheduler;
pub use scheduler::{DEFAULT_DRAFT_MS, MAX_STEP_TOKENS, Schedule};

/// Loads the model; runs on the engine thread, which then owns it.
pub type ModelLoader = Box<dyn FnOnce() -> Result<Box<dyn Model>> + Send>;

pub struct Job {
    pub prompt: Vec<u32>,
    /// Prompt positions worth a prefix checkpoint (e.g. where the last user message
    /// starts), so a later prompt sharing that prefix resumes there.
    pub reuse_at: Vec<usize>,
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

/// What a cached sequence offers a prompt that shares only part of its tokens.
pub trait Resume {
    /// Positions [`Resume::rewind_to`] can return to, ascending.
    fn checkpoints(&self) -> Vec<usize>;
    fn rewind_to(&mut self, pos: usize) -> Result<()>;
}

impl Resume for () {
    fn checkpoints(&self) -> Vec<usize> {
        Vec::new()
    }
    fn rewind_to(&mut self, _: usize) -> Result<()> {
        Ok(())
    }
}

impl Resume for Box<dyn Session> {
    fn checkpoints(&self) -> Vec<usize> {
        Session::checkpoints(&**self)
    }
    fn rewind_to(&mut self, pos: usize) -> Result<()> {
        Session::rewind_to(&mut **self, pos)
    }
}

/// How many tokens of `prompt` a sequence of `ids` (with or without the logits
/// after them, with prefix `checkpoints`) can serve: all of `ids` when the prompt
/// extends them, else the longest checkpoint inside the shared prefix that leaves
/// at least one prompt token to feed. `None` when nothing is reusable.
pub fn reusable(
    ids: &[u32],
    has_logits: bool,
    checkpoints: &[usize],
    prompt: &[u32],
) -> Option<usize> {
    if !ids.is_empty() && prompt.starts_with(ids) && (prompt.len() > ids.len() || has_logits) {
        return Some(ids.len());
    }
    let shared = ids.iter().zip(prompt).take_while(|(a, b)| a == b).count();
    checkpoints
        .iter()
        .copied()
        .filter(|&p| p > 0 && p <= shared && p < prompt.len())
        .max()
}

/// Returns `seq` to the first `pos` of its tokens (`pos` from [`reusable`]).
pub fn resume_at<S: Resume>(mut seq: CachedSeq<S>, pos: usize) -> Result<CachedSeq<S>> {
    if pos < seq.ids.len() {
        seq.state.rewind_to(pos)?;
        seq.ids.truncate(pos);
        seq.logits.clear();
    }
    Ok(seq)
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

impl<S: Resume> PrefixCache<S> for LastSequence<S> {
    fn take(&mut self, prompt: &[u32], _: &mut dyn FnMut() -> Result<S>) -> Option<CachedSeq<S>> {
        // An identical prompt needs the cached logits; a longer one only the state;
        // one that shares only a prefix, a checkpoint inside it.
        let pos = self
            .0
            .as_ref()
            .and_then(|c| reusable(&c.ids, !c.logits.is_empty(), &c.state.checkpoints(), prompt))?;
        let seq = self.0.take()?;
        match resume_at(seq, pos) {
            Ok(seq) => Some(seq),
            Err(e) => {
                eprintln!("oominf: cannot rewind the cached sequence: {e:#}");
                None
            }
        }
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
    telemetry: Arc<Mutex<Telemetry>>,
}

/// What the engine is doing (`/v1/stats`), updated by the engine thread as it
/// works: at a request's start, after prefill, every decode step and at the end.
#[derive(Debug, Clone, Default)]
pub struct Telemetry {
    pub requests_active: u64,
    pub requests_completed: u64,
    /// Tokens the current (or last) sequence holds, and the context limit.
    pub context_tokens: usize,
    pub max_context: usize,
    /// Generated tokens per second over about the last second of decoding.
    pub decode_tps: Option<f64>,
    /// Where routed experts came from so far, and how much each tier holds.
    pub experts: ExpertStats,
    pub tiers: ExpertTiers,
}

impl EngineHandle {
    /// A handle over any job queue (the engine thread, or a stand-in in tests).
    pub fn new(jobs: std_mpsc::Sender<Job>) -> Self {
        Self::with_telemetry(jobs, Arc::default())
    }

    fn with_telemetry(jobs: std_mpsc::Sender<Job>, telemetry: Arc<Mutex<Telemetry>>) -> Self {
        EngineHandle { jobs, telemetry }
    }

    /// A snapshot of the engine's telemetry.
    pub fn telemetry(&self) -> Telemetry {
        self.telemetry.lock().unwrap().clone()
    }

    pub fn submit(&self, job: Job) -> Result<()> {
        self.jobs
            .send(job)
            .map_err(|_| anyhow::anyhow!("engine is not running"))
    }
}

/// Starts the engine thread for sequences of up to `max_context` tokens, drafting
/// up to `draft` tokens per decode step when the model can (speculative decoding;
/// 0 disables it), sharing steps between requests as `sched` says. `ready`
/// receives the startup summary once the model is loaded, or the load error.
pub fn start(
    loader: ModelLoader,
    max_context: usize,
    draft: usize,
    lookup: usize,
    store: Option<StoreConfig>,
    sched: Schedule,
) -> (EngineHandle, std_mpsc::Receiver<Result<String>>) {
    let (jobs_tx, jobs_rx) = std_mpsc::channel::<Job>();
    let (ready_tx, ready_rx) = std_mpsc::channel();
    let telemetry = Arc::new(Mutex::new(Telemetry {
        max_context,
        ..Telemetry::default()
    }));
    let shared = telemetry.clone();
    std::thread::Builder::new()
        .name("oominf-engine".into())
        .spawn(move || {
            let engine =
                match Engine::load(loader, max_context, draft, lookup, store, sched, shared) {
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
    (EngineHandle::with_telemetry(jobs_tx, telemetry), ready_rx)
}

type Seq = Box<dyn Session>;

/// Prompts shorter than this are not worth a checkpoint (they prefill in moments).
const MIN_CHECKPOINT: usize = 1024;

/// Prefix checkpoints for a prefill of prompt positions `from..to`: the request's
/// reuse points (message starts) and powers of two from 8k tokens (documents and
/// question in one message), each costing about 0.1 GB of host memory.
fn checkpoint_plan(reuse_at: &[usize], from: usize, to: usize) -> Vec<usize> {
    let mut plan: Vec<usize> = reuse_at
        .iter()
        .copied()
        .chain((13..20).map(|k| 1usize << k))
        .filter(|&p| p > from && p < to && p >= MIN_CHECKPOINT)
        .collect();
    plan.sort_unstable();
    plan.dedup();
    plan
}

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
    /// Tokens per prompt-lookup draft (0: off).
    lookup: usize,
    /// How steps are shared between requests.
    sched: Schedule,
    telemetry: Arc<Mutex<Telemetry>>,
}

impl Engine {
    fn load(
        loader: ModelLoader,
        max_context: usize,
        draft: usize,
        lookup: usize,
        store: Option<StoreConfig>,
        sched: Schedule,
        telemetry: Arc<Mutex<Telemetry>>,
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
        let streams = if sched.max_streams > 1 {
            format!(
                "; up to {} streams, {} tokens per step, prefill slice {}",
                sched.max_streams,
                sched.step_cost.max_width(),
                sched.prefill_slice
            )
        } else {
            String::new()
        };
        let summary = format!(
            "model loaded in {:.1}s; {}; max context {max_context} tokens; draft {draft} tokens per step; prompt lookup {lookup} tokens{stored}{streams}",
            t.elapsed().as_secs_f64(),
            model.describe(),
        );
        Ok((
            Engine {
                model,
                cache,
                max_context,
                draft,
                lookup,
                sched,
                telemetry,
            },
            summary,
        ))
    }

    fn run(mut self, jobs: std_mpsc::Receiver<Job>) {
        if self.sched.max_streams > 1 {
            return self.run_batched(jobs);
        }
        self.publish(0, None);
        while let Ok(job) = jobs.recv() {
            let events = job.events.clone();
            self.telemetry.lock().unwrap().requests_active = 1;
            if let Err(e) = self.serve(job) {
                // The cached state may be half-updated: drop it unsaved.
                self.cache.discard();
                let _ = events.blocking_send(Event::Failed(format!("{e:#}")));
            }
            let mut t = self.telemetry.lock().unwrap();
            t.requests_active = 0;
            t.requests_completed += 1;
        }
    }

    /// Updates the telemetry: the sequence's length, the decode rate when one was
    /// measured, and the expert tiers' counters.
    fn publish(&self, context_tokens: usize, decode_tps: Option<f64>) {
        let (experts, tiers) = (self.model.expert_stats(), self.model.expert_tiers());
        let mut t = self.telemetry.lock().unwrap();
        t.context_tokens = context_tokens;
        if decode_tps.is_some() {
            t.decode_tps = decode_tps;
        }
        t.experts = experts;
        t.tiers = tiers;
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
        self.publish(cached, None);
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
        state.plan_checkpoints(&checkpoint_plan(&job.reuse_at, cached, job.prompt.len()));
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

        self.publish(ids.len(), None);
        // Emitted-token counts over about the last second, for the decode rate.
        let mut rate: VecDeque<(Instant, usize)> = VecDeque::new();
        let mut sampler = Sampler::new(job.sampling);
        let mut finish = FinishReason::Length;
        let (mut emitted, mut drafted, mut accepted) = (0, 0, 0);
        let (mut looked_up, mut lookup_accepted) = (0, 0);
        let mut lookup = (self.lookup > 0).then(|| PromptLookup::new(&LOOKUP_MATCH, self.lookup));
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
            // Prompt lookup first (free when the output repeats the sequence),
            // else the model's own draft.
            let looked = lookup.as_mut().map(|l| {
                ids.push(next);
                let d = l.draft(&ids);
                ids.pop();
                d
            });
            let from_lookup = looked.as_ref().is_some_and(|d| !d.is_empty());
            let d = decode_step_with(
                &mut *state,
                next,
                self.draft,
                looked,
                |row: &[f32], draft: Option<u32>| Ok(sampler.choose(row, draft)),
            )?;
            drafted += d.drafted;
            accepted += d.accepted;
            if from_lookup {
                looked_up += d.drafted;
                lookup_accepted += d.accepted;
                if let Some(l) = lookup.as_mut() {
                    l.record(d.drafted, d.accepted);
                }
            }
            let now = Instant::now();
            rate.push_back((now, emitted + d.tokens.len() - 1));
            while rate.len() > 2 && now - rate[1].0 >= Duration::from_secs(1) {
                rate.pop_front();
            }
            let tps = match (rate.front(), rate.back()) {
                (Some(&(t0, n0)), Some(&(t1, n1))) if t1 > t0 => {
                    Some((n1 - n0) as f64 / (t1 - t0).as_secs_f64())
                }
                _ => None,
            };
            self.publish(ids.len() + d.tokens.len(), tps);
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
                "oominf: {emitted} tokens, drafts accepted {accepted}/{drafted} ({:.0}%); prompt lookup {lookup_accepted}/{looked_up}",
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
    fn reusable_prefers_extension_then_longest_checkpoint_in_the_shared_prefix() {
        let ids = [1, 2, 3, 4, 5, 6];
        // An extension reuses everything; an identical prompt only with logits.
        assert_eq!(
            reusable(&ids, false, &[2, 4], &[1, 2, 3, 4, 5, 6, 7]),
            Some(6)
        );
        assert_eq!(reusable(&ids, true, &[], &ids), Some(6));
        // Diverging after 5 tokens: the longest checkpoint at or before 5.
        assert_eq!(reusable(&ids, false, &[2, 4], &[1, 2, 3, 4, 5, 9]), Some(4));
        assert_eq!(reusable(&ids, false, &[2, 4], &[1, 2, 3, 8]), Some(2));
        // A checkpoint must leave a token to feed, and nothing shared is nothing.
        assert_eq!(reusable(&ids, false, &[2, 4], &[1, 2]), None);
        assert_eq!(reusable(&ids, false, &[2, 4], &[9, 2, 3]), None);
    }

    /// A sequence with checkpoints that records where it was rewound to.
    struct Ckpts(Vec<usize>, Option<usize>);

    impl Resume for Ckpts {
        fn checkpoints(&self) -> Vec<usize> {
            self.0.clone()
        }
        fn rewind_to(&mut self, pos: usize) -> Result<()> {
            self.1 = Some(pos);
            Ok(())
        }
    }

    #[test]
    fn last_sequence_rewinds_to_a_checkpoint_for_a_diverging_prompt() {
        let mut c = LastSequence::default();
        c.put(CachedSeq {
            ids: vec![1, 2, 3, 4, 5, 6],
            state: Ckpts(vec![2, 4], None),
            logits: vec![0.5],
        });
        let s = c
            .take(&[1, 2, 3, 4, 7, 8], &mut || anyhow::bail!("no sessions"))
            .unwrap();
        assert_eq!(s.ids, vec![1, 2, 3, 4]);
        assert_eq!(s.state.1, Some(4));
        assert!(s.logits.is_empty(), "logits after a rewind are unknown");
    }

    #[test]
    fn checkpoint_plan_keeps_reuse_points_and_powers_of_two_inside_the_prefill() {
        assert_eq!(
            checkpoint_plan(&[500, 3000, 9000], 0, 20_000),
            vec![3000, 8192, 9000, 16_384]
        );
        // Positions already cached or at the end are not planned.
        assert_eq!(checkpoint_plan(&[3000], 3000, 8192), Vec::<usize>::new());
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
                reuse_at: Vec::new(),
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
        engine_with(draft, 1)
    }

    fn engine_with(draft: usize, max_streams: usize) -> EngineHandle {
        let (handle, ready) = start(
            Box::new(|| Ok(Box::new(Counting))),
            1024,
            draft,
            0,
            None,
            Schedule {
                max_streams,
                ..Schedule::default()
            },
        );
        ready.recv().unwrap().unwrap();
        handle
    }

    /// Submits a job and returns its event stream.
    fn submit(handle: &EngineHandle, prompt: &[u32], max_tokens: usize, stop: &[u32]) -> Rx {
        let (tx, rx) = mpsc::channel(64);
        handle
            .submit(Job {
                prompt: prompt.to_vec(),
                reuse_at: Vec::new(),
                sampling: SamplingParams {
                    temperature: 0.0,
                    ..Default::default()
                },
                max_tokens,
                stop_ids: stop.to_vec(),
                events: tx,
            })
            .unwrap();
        rx
    }

    type Rx = mpsc::Receiver<Event>;

    fn collect(mut rx: Rx) -> (usize, Vec<u32>, Option<FinishReason>) {
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

    #[test]
    fn concurrent_requests_each_get_their_own_tokens_and_limits() {
        for draft in [0, 2] {
            let h = engine_with(draft, 4);
            // Submitted together: they decode in the same steps.
            let rxs: Vec<Rx> = [(0u32, 10usize), (3, 4), (5, 7), (7, 1), (2, 6)]
                .iter()
                .map(|&(p, n)| submit(&h, &[p], n, &[]))
                .collect();
            for (rx, (p, n)) in
                rxs.into_iter()
                    .zip([(0u32, 10usize), (3, 4), (5, 7), (7, 1), (2, 6)])
            {
                let (_, tokens, finish) = collect(rx);
                let want: Vec<u32> = (1..=n as u32).map(|i| (p + i) % 8).collect();
                assert_eq!(tokens, want, "prompt {p}, draft {draft}");
                assert_eq!(finish, Some(FinishReason::Length));
            }
        }
    }

    #[test]
    fn batched_stops_and_limits_inside_a_step_and_caching() {
        let h = engine_with(3, 2);
        // A hits its limit within a step; B runs longer and stops on 3, inside a
        // later step.
        let a = submit(&h, &[0], 2, &[]);
        let b = submit(&h, &[4], 100, &[3]);
        let (_, ta, fa) = collect(a);
        assert_eq!(ta, [1, 2]);
        assert_eq!(fa, Some(FinishReason::Length));
        let (_, tb, fb) = collect(b);
        assert_eq!(tb, [5, 6, 7, 0, 1, 2]);
        assert_eq!(fb, Some(FinishReason::Stop));
        // B ended last: it is the cached sequence, holding exactly its prompt and
        // emitted tokens, and a request extending it resumes there.
        let (cached, tokens, _) = collect(submit(&h, &[4, 5, 6, 7, 0, 1, 2, 3], 2, &[]));
        assert_eq!(cached, 7);
        assert_eq!(tokens, [4, 5]);
    }

    #[test]
    fn a_closed_client_leaves_the_others_running() {
        let h = engine_with(1, 3);
        let gone = submit(&h, &[0], 50, &[]);
        let kept = submit(&h, &[1], 20, &[]);
        drop(gone);
        let (_, tokens, finish) = collect(kept);
        assert_eq!(tokens.len(), 20);
        assert_eq!(finish, Some(FinishReason::Length));
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
