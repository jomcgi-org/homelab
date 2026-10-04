//! Speculative decoding over any [`Session`]: the model drafts tokens, one step
//! verifies them all, and the longest prefix the target agrees with is kept.
//!
//! The target decides every token: a draft is kept only when the token chosen from
//! the target's logits at that position equals it, so greedy output is the same as
//! decoding one token at a time. Sampled output keeps the target distribution when
//! `choose` implements the speculative-sampling rule: keep the draft with the
//! target's probability of it, otherwise sample the target without it.

use anyhow::{Result, ensure};

use crate::Session;

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
    mut choose: impl FnMut(&[f32], Option<u32>) -> Result<u32>,
) -> Result<Decoded> {
    let drafts = if k > 0 {
        session.draft(next, k)?
    } else {
        Vec::new()
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
    ensure!(
        !logits.is_empty() && logits.len().is_multiple_of(feed.len()),
        "step_all returned {} logits for {} tokens",
        logits.len(),
        feed.len()
    );
    let vocab = logits.len() / feed.len();
    let mut tokens = Vec::with_capacity(feed.len());
    let mut rows = Vec::with_capacity(feed.len());
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
