//! Speculative decoding over any [`Session`]: the model drafts tokens, one step
//! verifies them all, and the longest prefix the target agrees with is kept.
//!
//! The target decides every token: a draft is kept only when the token chosen from
//! the target's logits at that position equals it, so greedy output is the same as
//! decoding one token at a time. Sampled output keeps the target distribution when
//! `choose` implements the speculative-sampling rule: keep the draft with the
//! target's probability of it, otherwise sample the target without it.

use anyhow::{Result, ensure};

use crate::{Feed, Model, Session};

/// What one [`decode_step`] produced.
#[derive(Debug, Clone, PartialEq)]
pub struct Decoded {
    /// Generated tokens in order. All but the last were fed to the session; the last
    /// is the next step's input.
    pub tokens: Vec<u32>,
    /// `rows[i]`: the logits `tokens[i]` was chosen from (after the session's
    /// `i`-th fed token of this step).
    pub rows: Vec<Vec<f32>>,
    /// Draft tokens proposed and how many of them the target accepted.
    pub drafted: usize,
    pub accepted: usize,
}

/// Feeds `next` (the last generated token) and up to `k` drafted tokens, and returns
/// the tokens they produce. `choose(logits, draft)` picks the token that follows a
/// position from that position's logits; `draft` is the drafted token for it, if
/// any, and a returned token equal to it accepts the draft.
pub fn decode_step(
    session: &mut dyn Session,
    next: u32,
    k: usize,
    choose: impl FnMut(&[f32], Option<u32>) -> Result<u32>,
) -> Result<Decoded> {
    decode_step_with(session, next, k, None, choose)
}

/// As [`decode_step`], verifying `drafts` (e.g. from [`PromptLookup`]) when given
/// and not empty, else up to `k` tokens the model drafts.
pub fn decode_step_with(
    session: &mut dyn Session,
    next: u32,
    k: usize,
    drafts: Option<Vec<u32>>,
    mut choose: impl FnMut(&[f32], Option<u32>) -> Result<u32>,
) -> Result<Decoded> {
    let drafts = match drafts {
        Some(d) if !d.is_empty() => d,
        _ if k > 0 => session.draft(next, k)?,
        _ => Vec::new(),
    };
    if drafts.is_empty() {
        let logits = session.step(&[next])?;
        return Ok(Decoded {
            tokens: vec![choose(&logits, None)?],
            rows: vec![logits],
            drafted: 0,
            accepted: 0,
        });
    }
    let mut feed = Vec::with_capacity(drafts.len() + 1);
    feed.push(next);
    feed.extend_from_slice(&drafts);
    let logits = session.step_all(&feed)?;
    accept(session, &drafts, &logits, choose)
}

/// What one sequence feeds to a [`decode_many`] step: the last generated token and
/// the drafts to verify after it (possibly none).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Proposal {
    pub next: u32,
    pub drafts: Vec<u32>,
}

/// [`decode_step_with`] for several sequences in one [`Model::step_many`] step:
/// each session is fed its proposal's `next` and drafts, keeps the longest prefix
/// of its drafts the model agrees with and rewinds the rest. `choose(i, logits,
/// draft)` picks sequence `i`'s token as in [`decode_step`].
pub fn decode_many(
    model: &dyn Model,
    sessions: &mut [&mut dyn Session],
    proposals: &[Proposal],
    mut choose: impl FnMut(usize, &[f32], Option<u32>) -> Result<u32>,
) -> Result<Vec<Decoded>> {
    ensure!(
        sessions.len() == proposals.len(),
        "{} sessions for {} proposals",
        sessions.len(),
        proposals.len()
    );
    let feeds_tokens: Vec<Vec<u32>> = proposals
        .iter()
        .map(|p| {
            std::iter::once(p.next)
                .chain(p.drafts.iter().copied())
                .collect()
        })
        .collect();
    let mut feeds: Vec<Feed<'_>> = sessions
        .iter_mut()
        .zip(&feeds_tokens)
        .map(|(s, tokens)| Feed {
            session: &mut **s,
            tokens,
        })
        .collect();
    let logits = model.step_many(&mut feeds)?;
    drop(feeds);
    ensure!(logits.len() == proposals.len(), "step_many lost a sequence");
    sessions
        .iter_mut()
        .zip(proposals)
        .zip(&logits)
        .enumerate()
        .map(|(i, ((s, p), l))| accept(&mut **s, &p.drafts, l, |row, d| choose(i, row, d)))
        .collect()
}

/// Keeps the longest prefix of `drafts` that `choose` agrees with, given the logits
/// after the step's next token and each draft, and rewinds the session past it.
fn accept(
    session: &mut dyn Session,
    drafts: &[u32],
    logits: &[f32],
    mut choose: impl FnMut(&[f32], Option<u32>) -> Result<u32>,
) -> Result<Decoded> {
    let fed = drafts.len() + 1;
    ensure!(
        !logits.is_empty() && logits.len().is_multiple_of(fed),
        "step returned {} logits for {fed} tokens",
        logits.len(),
    );
    let vocab = logits.len() / fed;
    let mut tokens = Vec::with_capacity(fed);
    let mut rows = Vec::with_capacity(fed);
    for (i, row) in logits.chunks(vocab).enumerate() {
        let draft = drafts.get(i).copied();
        let token = choose(row, draft)?;
        tokens.push(token);
        rows.push(row.to_vec());
        if draft != Some(token) {
            break;
        }
    }
    let accepted = tokens.len() - 1;
    session.rewind(drafts.len() - accepted)?;
    Ok(Decoded {
        tokens,
        rows,
        drafted: drafts.len(),
        accepted,
    })
}

/// Prompt-lookup drafting (#6872): output that copies earlier text (a file being
/// edited, quoted input, repeated code) is drafted from the sequence itself. The
/// last `n` tokens are matched against earlier positions, longest `n` first, and
/// the tokens that followed the most recent match are the draft. Free to compute;
/// the target still decides every token, so output is unchanged.
pub struct PromptLookup {
    /// Match lengths tried, longest first.
    ns: Vec<usize>,
    /// Most tokens per draft.
    k: usize,
    seq: Vec<u32>,
    /// Per match length: n-gram hash to its most recent position with a token
    /// after it (a hash collision only costs a rejected draft).
    index: Vec<std::collections::HashMap<u64, usize>>,
    /// Positions indexed so far, per match length.
    done: Vec<usize>,
    /// Recent fraction of drafted tokens kept (exponentially weighted), and
    /// rounds since a lookup draft was last offered while below the gate.
    rate: f32,
    skipped: usize,
}

/// Lookup drafts are offered while recent acceptance is at least this. An
/// 8-token verification costs ~120 ms against ~47 ms for a model-drafted step
/// keeping ~1.8 tokens, so a 7-token draft pays when about half of it is kept.
/// Measured on structured output (newline-delimited JSON) that matched short
/// scaffolding: lookup unconditionally cost 20% (37 vs 47 tok/s).
const LOOKUP_GATE: f32 = 0.55;
/// While below the gate, every this many rounds a short draft (`LOOKUP_PROBE_K`
/// tokens) is offered anyway, to notice when output starts copying.
const LOOKUP_PROBE: usize = 32;
const LOOKUP_PROBE_K: usize = 3;
/// A sequence starts just above the gate, so one mostly rejected draft closes it.
const LOOKUP_START: f32 = 0.6;
const LOOKUP_ALPHA: f32 = 0.3;

/// Matches of 8, 6 or 4 tokens: on recorded outputs, shorter matches drafted
/// novel text often enough to cost more than they saved.
pub const LOOKUP_MATCH: [usize; 3] = [8, 6, 4];

fn ngram_hash(tokens: &[u32]) -> u64 {
    tokens.iter().fold(0xcbf2_9ce4_8422_2325u64, |h, &t| {
        (h ^ t as u64).wrapping_mul(0x0000_0100_0000_01b3)
    })
}

impl PromptLookup {
    pub fn new(ns: &[usize], k: usize) -> Self {
        PromptLookup {
            ns: ns.to_vec(),
            k,
            seq: Vec::new(),
            index: ns.iter().map(|_| Default::default()).collect(),
            done: vec![0; ns.len()],
            rate: LOOKUP_START,
            skipped: 0,
        }
    }

    /// Records how many of a lookup draft's tokens the model kept.
    pub fn record(&mut self, drafted: usize, kept: usize) {
        if drafted > 0 {
            let r = kept as f32 / drafted as f32;
            self.rate = (1.0 - LOOKUP_ALPHA) * self.rate + LOOKUP_ALPHA * r;
        }
    }

    /// A draft continuing `history` (every token so far, the last one not yet fed),
    /// empty when nothing matches or recent drafts were mostly rejected (see
    /// [`PromptLookup::record`]). `history` normally extends the previous call's;
    /// otherwise the index is rebuilt.
    pub fn draft(&mut self, history: &[u32]) -> Vec<u32> {
        if !history.starts_with(&self.seq) {
            self.seq.clear();
            self.index.iter_mut().for_each(|m| m.clear());
            self.done.iter_mut().for_each(|d| *d = 0);
        }
        self.seq.extend_from_slice(&history[self.seq.len()..]);
        let len = self.seq.len();
        for (i, &n) in self.ns.iter().enumerate() {
            // Positions with a token after them, so the suffix never matches itself.
            for j in self.done[i]..len.saturating_sub(n) {
                self.index[i].insert(ngram_hash(&self.seq[j..j + n]), j);
            }
            self.done[i] = self.done[i].max(len.saturating_sub(n));
        }
        // Below the gate, offer a short draft only every LOOKUP_PROBE rounds.
        let mut k = self.k;
        if self.rate < LOOKUP_GATE {
            self.skipped += 1;
            if self.skipped < LOOKUP_PROBE {
                return Vec::new();
            }
            self.skipped = 0;
            k = k.min(LOOKUP_PROBE_K);
        }
        for (i, &n) in self.ns.iter().enumerate() {
            if len < n {
                continue;
            }
            if let Some(&pos) = self.index[i].get(&ngram_hash(&self.seq[len - n..])) {
                let from = pos + n;
                return self.seq[from..(from + k).min(len)].to_vec();
            }
        }
        Vec::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Session;

    /// A session over a fixed table: the logits after token `x` peak at `table[x]`;
    /// it drafts by following `drafts` and records what it was fed.
    struct Table {
        table: Vec<u32>,
        drafts: Vec<u32>,
        fed: Vec<u32>,
        rewound: usize,
    }

    impl Table {
        fn row(&self, x: u32) -> Vec<f32> {
            let mut r = vec![0.0; self.table.len()];
            r[self.table[x as usize] as usize] = 1.0;
            r
        }
    }

    impl Session for Table {
        fn len(&self) -> usize {
            self.fed.len()
        }
        fn prefill(&mut self, tokens: &[u32], _: &dyn Fn() -> bool) -> Result<Option<Vec<f32>>> {
            self.fed.extend(tokens);
            Ok(Some(self.row(*tokens.last().unwrap())))
        }
        fn step(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
            self.fed.extend(tokens);
            Ok(self.row(*tokens.last().unwrap()))
        }
        fn step_all(&mut self, tokens: &[u32]) -> Result<Vec<f32>> {
            self.fed.extend(tokens);
            Ok(tokens.iter().flat_map(|&t| self.row(t)).collect())
        }
        fn rewind(&mut self, n: usize) -> Result<()> {
            self.fed.truncate(self.fed.len() - n);
            self.rewound += n;
            Ok(())
        }
        fn draft(&mut self, _next: u32, k: usize) -> Result<Vec<u32>> {
            Ok(self.drafts.iter().take(k).copied().collect())
        }
    }

    fn greedy(row: &[f32], _: Option<u32>) -> Result<u32> {
        Ok(row
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .unwrap()
            .0 as u32)
    }

    // Successor table: 0 -> 1 -> 2 -> 3 -> 4 -> 0.
    fn session(drafts: Vec<u32>) -> Table {
        Table {
            table: vec![1, 2, 3, 4, 0],
            drafts,
            fed: Vec::new(),
            rewound: 0,
        }
    }

    #[test]
    fn prompt_lookup_drafts_what_followed_the_longest_match() {
        let mut l = PromptLookup::new(&[4, 2], 3);
        // "1 2 3 4 5 6 7" then "9 3 4": the 2-token suffix "3 4" matches; 5 6 7 follow.
        let mut h = vec![1, 2, 3, 4, 5, 6, 7, 9, 3, 4];
        assert_eq!(l.draft(&h), vec![5, 6, 7]);
        // No 4- or 2-token match: no draft.
        h.extend([11, 12]);
        assert_eq!(l.draft(&h), Vec::<u32>::new());
        // A 4-token match wins over a more recent 2-token one.
        let mut l = PromptLookup::new(&[4, 2], 2);
        let h = vec![1, 2, 3, 4, 50, 51, 3, 4, 60, 1, 2, 3, 4];
        assert_eq!(l.draft(&h), vec![50, 51]);
        // A history that does not extend the last one rebuilds the index.
        assert_eq!(l.draft(&[7, 8, 9, 7, 8]), vec![9, 7]);
    }

    #[test]
    fn all_drafts_accepted_gives_a_bonus_token() {
        let mut s = session(vec![2, 3]);
        let d = decode_step(&mut s, 1, 2, greedy).unwrap();
        assert_eq!(d.tokens, [2, 3, 4]);
        assert_eq!((d.drafted, d.accepted), (2, 2));
        assert_eq!(s.fed, [1, 2, 3]);
        assert_eq!(s.rewound, 0);
    }

    #[test]
    fn first_mismatch_stops_and_rewinds_the_rest() {
        let mut s = session(vec![2, 0, 1]);
        let d = decode_step(&mut s, 1, 3, greedy).unwrap();
        // After 2 the target says 3, not the drafted 0: keep [2], correct to 3.
        assert_eq!(d.tokens, [2, 3]);
        assert_eq!((d.drafted, d.accepted), (3, 1));
        assert_eq!(s.fed, [1, 2]);
        assert_eq!(s.rewound, 2);
    }

    #[test]
    fn rejected_first_draft_equals_plain_decoding() {
        let mut s = session(vec![4]);
        let d = decode_step(&mut s, 1, 1, greedy).unwrap();
        assert_eq!(d.tokens, [2]);
        assert_eq!(s.fed, [1]);
    }

    /// A model whose sessions are [`Table`]s, batched by the default `step_many`.
    struct Tables;

    impl crate::Model for Tables {
        fn model_type(&self) -> &str {
            "tables"
        }
        fn vocab(&self) -> usize {
            5
        }
        fn new_session(&self, _: usize) -> Result<Box<dyn Session>> {
            Ok(Box::new(session(vec![])))
        }
        fn describe(&self) -> String {
            String::new()
        }
        fn expert_stats(&self) -> crate::ExpertStats {
            Default::default()
        }
    }

    #[test]
    fn decode_many_verifies_each_sequence_as_alone() {
        let (mut a, mut b, mut c) = (session(vec![]), session(vec![]), session(vec![]));
        let proposals = [
            // 2 is right, 0 is not: keep [2], correct to 3, rewind one.
            Proposal {
                next: 1,
                drafts: vec![2, 0],
            },
            Proposal {
                next: 3,
                drafts: vec![],
            },
            Proposal {
                next: 4,
                drafts: vec![0, 1],
            },
        ];
        let mut seen = Vec::new();
        let d = decode_many(
            &Tables,
            &mut [&mut a, &mut b, &mut c],
            &proposals,
            |i, row, draft| {
                seen.push(i);
                greedy(row, draft)
            },
        )
        .unwrap();
        assert_eq!(d[0].tokens, [2, 3]);
        assert_eq!((d[0].drafted, d[0].accepted), (2, 1));
        assert_eq!((a.fed.as_slice(), a.rewound), (&[1, 2][..], 1));
        assert_eq!(d[1].tokens, [4]);
        assert_eq!(b.fed, [3]);
        assert_eq!(d[2].tokens, [0, 1, 2]);
        assert_eq!((c.fed.as_slice(), c.rewound), (&[4, 0, 1][..], 0));
        assert_eq!(seen, [0, 0, 1, 2, 2, 2]);
    }

    #[test]
    fn no_draft_head_decodes_one_token() {
        let mut s = session(vec![]);
        let d = decode_step(&mut s, 3, 2, greedy).unwrap();
        assert_eq!(d.tokens, [4]);
        assert_eq!((d.drafted, d.accepted), (0, 0));
    }
}

/// Greedy choice: the index of the largest logit, the lowest index on ties.
pub fn argmax(logits: &[f32]) -> u32 {
    logits
        .iter()
        .enumerate()
        .max_by(|a, b| a.1.total_cmp(b.1).then(b.0.cmp(&a.0)))
        .map_or(0, |(i, _)| i as u32)
}

#[cfg(test)]
mod argmax_tests {
    use super::argmax;

    #[test]
    fn picks_the_max_and_the_lowest_index_on_ties() {
        assert_eq!(argmax(&[1.0, 3.0, 2.0]), 1);
        assert_eq!(argmax(&[3.0, 1.0, 3.0]), 0);
        assert_eq!(argmax(&[]), 0);
    }
}
