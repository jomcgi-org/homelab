//! Continuous batching: several requests decode at once, their tokens sharing
//! each step ([`Model::step_many`]), so a layer fetches the union of their routed
//! experts once. Requests join and leave between steps; a prompt prefills in
//! slices between other requests' decode steps; a request is admitted only while
//! its sequence fits in device memory beside the others (otherwise it queues).
//! Which drafts ride in a step beside every stream's next token is the token
//! budget's call ([`crate::budget`]).
//!
//! Each request keeps its own sampler, stop tokens, token limit, events and
//! cancellation, and its sequence goes to the prefix cache when it ends, exactly
//! as in the one-request engine.

use std::collections::VecDeque;
use std::sync::mpsc as std_mpsc;
use std::time::{Duration, Instant};

use anyhow::Result;
use oominf_core::{
    Decoded, LOOKUP_MATCH, PromptLookup, Proposal, Session, decode_many, decode_step_with,
};

use super::{CachedSeq, Engine, Event, FinishReason, Job, Seq, checkpoint_plan};
use crate::budget::{Acceptance, CostCurve, DEFAULT_STEP_COST, Offer, allocate};
use crate::sampling::Sampler;

/// How the engine shares steps between concurrent requests.
#[derive(Debug, Clone)]
pub struct Schedule {
    /// Requests decoding at once; 1 serves one request at a time, to completion.
    pub max_streams: usize,
    /// Step cost by width; its last width is the most tokens a step carries.
    pub step_cost: CostCurve,
    /// Milliseconds the model takes to draft one token (refined as it drafts).
    pub draft_ms: f64,
    /// Prompt tokens prefilled at a time while other requests decode (0: a whole
    /// prompt at once). A prompt prefills whole when nothing else is decoding.
    pub prefill_slice: usize,
}

/// Most tokens a batched step carries by default: the decode GEMV's limit
/// (wider steps run prefill GEMMs, which cost far more per step).
pub const MAX_STEP_TOKENS: usize = 16;

/// Prior cost of drafting one token with the model's draft head.
pub const DEFAULT_DRAFT_MS: f64 = 3.0;

impl Default for Schedule {
    fn default() -> Self {
        Schedule {
            max_streams: 1,
            step_cost: CostCurve::parse(DEFAULT_STEP_COST, MAX_STEP_TOKENS)
                .expect("default step cost parses"),
            draft_ms: DEFAULT_DRAFT_MS,
            prefill_slice: 0,
        }
    }
}

/// Weight of a new measurement in the drafting cost.
const DRAFT_EMA: f64 = 0.1;

/// How often the batched engine logs its steps.
const REPORT_EVERY: Duration = Duration::from_secs(30);

/// Why a stream left.
enum End {
    /// Generation finished: the sequence is cached and the client told.
    Finished(FinishReason),
    /// The client went away between steps: the sequence is still consistent and
    /// is cached.
    Gone,
    /// Cancelled mid-prefill: the sequence is partially advanced and dropped.
    Discard,
}

/// One request being served.
struct Stream {
    job: Job,
    max_tokens: usize,
    /// Tokens the session holds: the prompt (once prefilled) and emitted tokens.
    ids: Vec<u32>,
    state: Seq,
    /// Logits after the last token of `ids` (empty while unknown).
    logits: Vec<f32>,
    /// Whether the prompt is prefilled and `next` holds the token to feed next.
    decoding: bool,
    next: u32,
    sampler: Sampler,
    emitted: usize,
    lookup: Option<PromptLookup>,
    /// Acceptance of the model's drafts and of prompt-lookup drafts.
    model_acc: Acceptance,
    lookup_acc: Acceptance,
    drafted: usize,
    accepted: usize,
    looked_up: usize,
    lookup_accepted: usize,
    admitted: Instant,
}

impl Stream {
    /// Emits `next`, the token the model chose and the sequence has not been fed:
    /// ends the stream on a stop token, a closed client or the token limit.
    fn emit_next(&mut self) -> Option<End> {
        if self.job.stop_ids.contains(&self.next) {
            return Some(End::Finished(FinishReason::Stop));
        }
        if self
            .job
            .events
            .blocking_send(Event::Token(self.next))
            .is_err()
        {
            return Some(End::Gone);
        }
        self.emitted += 1;
        (self.emitted == self.max_tokens).then_some(End::Finished(FinishReason::Length))
    }

    /// Takes a decode step's tokens: `tokens[..n - 1]` are accepted drafts, already
    /// fed; `tokens[n - 1]` is the new `next`. A stop, a closed client or the token
    /// limit inside the step cuts the sequence back to what was emitted.
    fn absorb(&mut self, d: &Decoded) -> Result<Option<End>> {
        self.ids.push(self.next);
        let n = d.tokens.len();
        for (i, &tok) in d.tokens.iter().enumerate() {
            self.logits.clone_from(&d.rows[i]);
            if i + 1 == n {
                self.next = tok;
                return Ok(self.emit_next());
            }
            if self.job.stop_ids.contains(&tok) {
                self.state.rewind(n - 1 - i)?;
                return Ok(Some(End::Finished(FinishReason::Stop)));
            }
            if self.job.events.blocking_send(Event::Token(tok)).is_err() {
                self.state.rewind(n - 1 - i)?;
                return Ok(Some(End::Gone));
            }
            self.ids.push(tok);
            self.emitted += 1;
            if self.emitted == self.max_tokens {
                self.state.rewind(n - 2 - i)?;
                self.logits.clone_from(&d.rows[i + 1]);
                return Ok(Some(End::Finished(FinishReason::Length)));
            }
        }
        anyhow::bail!("a decode step produced no token")
    }

    /// A prompt-lookup draft continuing the sequence and `next`, if any.
    fn looked_up(&mut self) -> Option<Vec<u32>> {
        let l = self.lookup.as_mut()?;
        self.ids.push(self.next);
        let d = l.draft(&self.ids);
        self.ids.pop();
        Some(d)
    }

    /// Records a verified step's drafts.
    fn record(&mut self, d: &Decoded, from_lookup: bool) {
        self.drafted += d.drafted;
        self.accepted += d.accepted;
        if from_lookup {
            self.looked_up += d.drafted;
            self.lookup_accepted += d.accepted;
            self.lookup_acc.record(d.drafted, d.accepted);
            // The drafter's own gate (it stops offering drafts that keep missing).
            if let Some(l) = self.lookup.as_mut() {
                l.record(d.drafted, d.accepted);
            }
        } else {
            self.model_acc.record(d.drafted, d.accepted);
        }
    }

    /// Tokens the sequence will hold at most.
    fn target(&self, max_context: usize) -> usize {
        (self.job.prompt.len() + self.max_tokens).min(max_context)
    }
}

/// Step statistics for the periodic log line.
#[derive(Default)]
struct Steps {
    steps: usize,
    width: usize,
    streams: usize,
    tokens: usize,
    ms: f64,
}

impl Engine {
    /// Serves jobs with continuous batching until the job queue closes.
    pub(super) fn run_batched(mut self, jobs: std_mpsc::Receiver<Job>) {
        self.publish(0, None);
        let mut active: Vec<Stream> = Vec::new();
        let mut queue: VecDeque<Job> = VecDeque::new();
        let mut open = true;
        let mut rate: VecDeque<(Instant, usize)> = VecDeque::new();
        let mut generated = 0usize;
        let mut steps = Steps::default();
        let mut reported = Instant::now();
        loop {
            if active.is_empty() && queue.is_empty() {
                if !open {
                    break;
                }
                match jobs.recv() {
                    Ok(j) => queue.push_back(j),
                    Err(_) => break,
                }
            }
            loop {
                match jobs.try_recv() {
                    Ok(j) => queue.push_back(j),
                    Err(std_mpsc::TryRecvError::Empty) => break,
                    Err(std_mpsc::TryRecvError::Disconnected) => {
                        open = false;
                        break;
                    }
                }
            }
            queue.retain(|j| !j.events.is_closed());
            self.admit_from(&mut queue, &mut active);
            self.telemetry.lock().unwrap().requests_active = (active.len() + queue.len()) as u64;

            // One prefill slice (oldest first), then one decode step for everyone
            // already decoding.
            if let Some(i) = active.iter().position(|s| !s.decoding) {
                let others = active.iter().any(|s| s.decoding);
                match self.prefill_some(&mut active[i], others) {
                    Ok(None) => {}
                    Ok(Some(end)) => {
                        let s = active.remove(i);
                        self.retire(s, end);
                    }
                    Err(e) => {
                        let s = active.remove(i);
                        self.fail(s, &e);
                    }
                }
            }
            let before = steps.tokens;
            match self.decode_round(&mut active, &mut steps) {
                Ok(ends) => {
                    generated += steps.tokens - before;
                    for (i, end) in ends.into_iter().rev() {
                        let s = active.remove(i);
                        self.retire(s, end);
                    }
                }
                Err(e) => {
                    // Every decoding stream's state may be half-updated.
                    let (failed, kept): (Vec<_>, Vec<_>) =
                        active.into_iter().partition(|s| s.decoding);
                    active = kept;
                    for s in failed {
                        self.fail(s, &e);
                    }
                }
            }
            let now = Instant::now();
            rate.push_back((now, generated));
            while rate.len() > 2 && now - rate[1].0 >= Duration::from_secs(1) {
                rate.pop_front();
            }
            let tps = match (rate.front(), rate.back()) {
                (Some(&(t0, n0)), Some(&(t1, n1))) if t1 > t0 => {
                    Some((n1 - n0) as f64 / (t1 - t0).as_secs_f64())
                }
                _ => None,
            };
            self.publish(active.iter().map(|s| s.ids.len()).sum(), tps);
            if now - reported >= REPORT_EVERY && steps.steps > 0 {
                eprintln!(
                    "oominf: batched decode: {} steps, {:.1} streams and {:.1} tokens per step, {:.1} ms/step, {:.1} tok/s; step cost {}; drafting {:.1} ms/token",
                    steps.steps,
                    steps.streams as f64 / steps.steps as f64,
                    steps.width as f64 / steps.steps as f64,
                    steps.ms / steps.steps as f64,
                    1e3 * steps.tokens as f64 / steps.ms.max(1e-9),
                    self.sched.step_cost.describe(),
                    self.sched.draft_ms,
                );
                steps = Steps::default();
                reported = now;
            }
        }
    }

    /// Admits queued jobs, oldest first, while there is room for another stream
    /// and its sequence fits in device memory beside the active ones (one stream
    /// is always admitted, as the one-request engine does).
    fn admit_from(&mut self, queue: &mut VecDeque<Job>, active: &mut Vec<Stream>) {
        while active.len() < self.sched.max_streams {
            let Some(job) = queue.front() else { break };
            if !active.is_empty() && !self.fits(active, job) {
                break;
            }
            let job = queue.pop_front().expect("front exists");
            let events = job.events.clone();
            match self.admit(job) {
                Ok(Some(s)) => active.push(s),
                Ok(None) => {}
                Err(e) => {
                    let _ = events.blocking_send(Event::Failed(format!("{e:#}")));
                    self.telemetry.lock().unwrap().requests_completed += 1;
                }
            }
        }
    }

    /// Whether `job`'s sequence, grown to its limit, fits in the device memory
    /// left once the active sequences have grown to theirs.
    fn fits(&self, active: &[Stream], job: &Job) -> bool {
        let m = &self.model;
        let target = (job.prompt.len() + job.max_tokens).min(self.max_context);
        let growth: usize = active
            .iter()
            .map(|s| {
                m.sequence_bytes(s.target(self.max_context))
                    .saturating_sub(m.sequence_bytes(s.ids.len().max(1)))
            })
            .sum();
        m.sequence_bytes(target) + growth <= m.available_bytes()
    }

    /// Starts serving `job`: takes a reusable cached sequence or a fresh one and
    /// tells the client. `None` when the client is already gone.
    fn admit(&mut self, job: Job) -> Result<Option<Stream>> {
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
        let (ids, mut state, logits) = match restored {
            Some(c) => (c.ids, c.state, c.logits),
            None => {
                self.cache.clear();
                (Vec::new(), self.model.new_session(max_context)?, Vec::new())
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
            self.keep(CachedSeq { ids, state, logits });
            return Ok(None);
        }
        state.plan_checkpoints(&checkpoint_plan(&job.reuse_at, cached, job.prompt.len()));
        let sampler = Sampler::new(job.sampling.clone());
        Ok(Some(Stream {
            job,
            max_tokens,
            ids,
            state,
            logits,
            decoding: false,
            next: 0,
            sampler,
            emitted: 0,
            lookup: None,
            model_acc: Acceptance::default(),
            lookup_acc: Acceptance::default(),
            drafted: 0,
            accepted: 0,
            looked_up: 0,
            lookup_accepted: 0,
            admitted: Instant::now(),
        }))
    }

    /// Prefills the next slice of `s`'s prompt (all of it when no other stream
    /// is decoding); once the prompt is in, chooses and emits its first token.
    fn prefill_some(&mut self, s: &mut Stream, others: bool) -> Result<Option<End>> {
        let fresh = &s.job.prompt[s.ids.len()..];
        if fresh.is_empty() {
            anyhow::ensure!(!s.logits.is_empty(), "cached sequence has no logits");
        } else {
            let slice = self.sched.prefill_slice;
            let n = if others && slice > 0 {
                slice.min(fresh.len())
            } else {
                fresh.len()
            };
            let events = &s.job.events;
            match s.state.prefill(&fresh[..n], &|| events.is_closed())? {
                Some(l) => {
                    s.ids.extend_from_slice(&fresh[..n]);
                    s.logits = l;
                    if s.ids.len() < s.job.prompt.len() {
                        return Ok(None);
                    }
                }
                None => return Ok(Some(End::Discard)),
            }
        }
        s.decoding = true;
        s.next = s.sampler.sample(&s.logits);
        s.lookup = (self.lookup > 0).then(|| PromptLookup::new(&LOOKUP_MATCH, self.lookup));
        Ok(s.emit_next())
    }

    /// One decode step for every decoding stream. Returns the streams that ended
    /// (indices into `active`, ascending).
    fn decode_round(
        &mut self,
        active: &mut [Stream],
        steps: &mut Steps,
    ) -> Result<Vec<(usize, End)>> {
        let idx: Vec<usize> = (0..active.len()).filter(|&i| active[i].decoding).collect();
        let mut ends = Vec::new();
        if idx.is_empty() {
            return Ok(ends);
        }
        if let [i] = idx[..] {
            // One stream: exactly the one-request engine's step.
            let s = &mut active[i];
            let looked = s.looked_up();
            let from_lookup = looked.as_ref().is_some_and(|d| !d.is_empty());
            let t = Instant::now();
            let sampler = &mut s.sampler;
            let d = decode_step_with(&mut *s.state, s.next, self.draft, looked, |row, draft| {
                Ok(sampler.choose(row, draft))
            })?;
            let ms = t.elapsed().as_secs_f64() * 1e3;
            steps.add(1, d.drafted + 1, d.tokens.len(), ms);
            s.record(&d, from_lookup);
            if let Some(end) = s.absorb(&d)? {
                ends.push((i, end));
            }
            return Ok(ends);
        }
        // Several: the token budget decides each stream's drafts.
        let looked: Vec<Option<Vec<u32>>> = idx.iter().map(|&i| active[i].looked_up()).collect();
        let offers: Vec<Offer> = idx
            .iter()
            .zip(&looked)
            .map(|(&i, l)| Offer {
                free: l.as_ref().map_or(0, Vec::len),
                free_rate: active[i].lookup_acc.rate(),
                model: self.draft,
                model_rate: active[i].model_acc.rate(),
            })
            .collect();
        let plan = allocate(&offers, &self.sched.step_cost, self.sched.draft_ms);
        let mut proposals = Vec::with_capacity(idx.len());
        for (j, &i) in idx.iter().enumerate() {
            let s = &mut active[i];
            let k = plan.drafts[j];
            let drafts = if k == 0 {
                Vec::new()
            } else if plan.free[j] {
                looked[j].as_ref().map_or(Vec::new(), |d| d[..k].to_vec())
            } else {
                let t = Instant::now();
                let d = s.state.draft(s.next, k)?;
                if !d.is_empty() {
                    let ms = t.elapsed().as_secs_f64() * 1e3 / d.len() as f64;
                    self.sched.draft_ms += DRAFT_EMA * (ms - self.sched.draft_ms);
                }
                d
            };
            proposals.push(Proposal {
                next: s.next,
                drafts,
            });
        }
        let width: usize = proposals.iter().map(|p| p.drafts.len() + 1).sum();
        let t = Instant::now();
        let decoded = {
            let mut sessions: Vec<&mut dyn Session> = Vec::with_capacity(idx.len());
            let mut samplers: Vec<&mut Sampler> = Vec::with_capacity(idx.len());
            for s in active.iter_mut().filter(|s| s.decoding) {
                sessions.push(&mut *s.state);
                samplers.push(&mut s.sampler);
            }
            decode_many(&*self.model, &mut sessions, &proposals, |j, row, draft| {
                Ok(samplers[j].choose(row, draft))
            })?
        };
        let ms = t.elapsed().as_secs_f64() * 1e3;
        self.sched.step_cost.observe(width, ms);
        let tokens: usize = decoded.iter().map(|d| d.tokens.len()).sum();
        steps.add(idx.len(), width, tokens, ms);
        if std::env::var_os("OOMINF_TRACE_STEPS").is_some() {
            eprintln!(
                "oominf: step of {} streams, {width} tokens, {tokens} kept, {ms:.1} ms",
                idx.len()
            );
        }
        for ((j, &i), d) in idx.iter().enumerate().zip(&decoded) {
            let s = &mut active[i];
            s.record(d, plan.free[j] && plan.drafts[j] > 0);
            if let Some(end) = s.absorb(d)? {
                ends.push((i, end));
            }
        }
        Ok(ends)
    }

    /// Ends a stream: caches its sequence unless it was discarded, and tells the
    /// client when generation finished.
    fn retire(&mut self, s: Stream, end: End) {
        if s.drafted > 0 && matches!(end, End::Finished(_)) {
            eprintln!(
                "oominf: {} tokens in {:.1}s, drafts accepted {}/{} ({:.0}%); prompt lookup {}/{}",
                s.emitted,
                s.admitted.elapsed().as_secs_f64(),
                s.accepted,
                s.drafted,
                100.0 * s.accepted as f64 / s.drafted as f64,
                s.lookup_accepted,
                s.looked_up
            );
        }
        let events = s.job.events.clone();
        let mut t = self.telemetry.lock().unwrap();
        t.requests_completed += 1;
        drop(t);
        match end {
            End::Discard => {}
            End::Gone => self.keep(CachedSeq {
                ids: s.ids,
                state: s.state,
                logits: s.logits,
            }),
            End::Finished(reason) => {
                self.keep(CachedSeq {
                    ids: s.ids,
                    state: s.state,
                    logits: s.logits,
                });
                let _ = events.blocking_send(Event::Finished(reason));
            }
        }
    }

    /// Ends a stream whose step failed: its state may be half-updated, so it is
    /// dropped, and the client is told.
    fn fail(&mut self, s: Stream, e: &anyhow::Error) {
        let _ = s.job.events.blocking_send(Event::Failed(format!("{e:#}")));
        self.telemetry.lock().unwrap().requests_completed += 1;
    }

    /// Caches `seq`. Concurrent streams end one after another, so the cache may
    /// hold another stream's sequence: clearing first saves it (a prefix store)
    /// rather than dropping it unsaved.
    fn keep(&mut self, seq: CachedSeq<Seq>) {
        self.cache.clear();
        super::keep(&mut *self.cache, seq);
    }
}

impl Steps {
    fn add(&mut self, streams: usize, width: usize, tokens: usize, ms: f64) {
        self.steps += 1;
        self.streams += streams;
        self.width += width;
        self.tokens += tokens;
        self.ms += ms;
    }
}
