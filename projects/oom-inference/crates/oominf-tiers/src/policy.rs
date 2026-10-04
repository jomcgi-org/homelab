//! Replacement policies for a fixed-capacity cache of expert records.
//!
//! A policy only ranks resident keys; the cache owns placement. Keys are
//! `layer * num_experts + expert`.

use std::collections::{BTreeSet, HashMap};

/// Ranks resident keys for eviction.
pub trait Policy: Send {
    /// `key` was accessed (hit or freshly inserted) at logical time `now`.
    fn touch(&mut self, key: u32, now: u64);
    /// `key` left the cache.
    fn remove(&mut self, key: u32);
    /// The best victim among resident keys for which `pinned` is false.
    fn victim(&self, pinned: &dyn Fn(u32) -> bool) -> Option<u32>;
    fn name(&self) -> String;
}

/// Least recently used.
#[derive(Default)]
pub struct Lru {
    last: HashMap<u32, u64>,
    order: BTreeSet<(u64, u32)>,
}

impl Policy for Lru {
    fn touch(&mut self, key: u32, now: u64) {
        if let Some(old) = self.last.insert(key, now) {
            self.order.remove(&(old, key));
        }
        self.order.insert((now, key));
    }

    fn remove(&mut self, key: u32) {
        if let Some(old) = self.last.remove(&key) {
            self.order.remove(&(old, key));
        }
    }

    fn victim(&self, pinned: &dyn Fn(u32) -> bool) -> Option<u32> {
        self.order.iter().map(|&(_, k)| k).find(|&k| !pinned(k))
    }

    fn name(&self) -> String {
        "lru".into()
    }
}

/// LRFU: each access adds `2^(now / half_life)` to a key's score, so a score is an
/// access count with exponential decay of half-life `half_life` accesses. Small
/// half-lives behave like LRU, large ones like LFU. Scores are kept in log2 space so
/// they never overflow.
pub struct Lrfu {
    half_life: f64,
    score: HashMap<u32, f64>,
    order: BTreeSet<(OrdF64, u32)>,
}

/// An `f64` ordered by `total_cmp`, so it can key ordered collections.
#[derive(Clone, Copy)]
struct OrdF64(f64);

impl Ord for OrdF64 {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.0.total_cmp(&other.0)
    }
}

impl PartialOrd for OrdF64 {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq for OrdF64 {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other).is_eq()
    }
}

impl Eq for OrdF64 {}

impl Lrfu {
    pub fn new(half_life: f64) -> Self {
        Lrfu {
            half_life,
            score: HashMap::new(),
            order: BTreeSet::new(),
        }
    }
}

/// log2(2^a + 2^b), stable.
fn log2_add(a: f64, b: f64) -> f64 {
    let (hi, lo) = if a > b { (a, b) } else { (b, a) };
    hi + (1.0 + (lo - hi).exp2()).log2()
}

impl Policy for Lrfu {
    fn touch(&mut self, key: u32, now: u64) {
        let add = now as f64 / self.half_life;
        let new = match self.score.get(&key) {
            Some(&old) => {
                self.order.remove(&(OrdF64(old), key));
                log2_add(old, add)
            }
            None => add,
        };
        self.score.insert(key, new);
        self.order.insert((OrdF64(new), key));
    }

    fn remove(&mut self, key: u32) {
        if let Some(old) = self.score.remove(&key) {
            self.order.remove(&(OrdF64(old), key));
        }
    }

    fn victim(&self, pinned: &dyn Fn(u32) -> bool) -> Option<u32> {
        self.order.iter().map(|&(_, k)| k).find(|&k| !pinned(k))
    }

    fn name(&self) -> String {
        format!("lrfu(half_life={})", self.half_life)
    }
}

/// Parses `lru`, `lfu` (LRFU with a very long half-life) or `lrfu:<half_life>`.
pub fn parse(spec: &str) -> anyhow::Result<Box<dyn Policy>> {
    Ok(match spec {
        "lru" => Box::new(Lru::default()),
        "lfu" => Box::new(Lrfu::new(1e12)),
        s => match s.strip_prefix("lrfu:") {
            Some(h) => Box::new(Lrfu::new(h.parse()?)),
            None => anyhow::bail!("unknown policy {spec:?} (lru, lfu, lrfu:<half_life>)"),
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lru_evicts_oldest_unpinned() {
        let mut p = Lru::default();
        for (t, k) in [1, 2, 3].iter().enumerate() {
            p.touch(*k, t as u64);
        }
        p.touch(1, 10);
        assert_eq!(p.victim(&|_| false), Some(2));
        assert_eq!(p.victim(&|k| k == 2), Some(3));
        p.remove(2);
        assert_eq!(p.victim(&|_| false), Some(3));
    }

    #[test]
    fn lrfu_prefers_frequent_and_decays() {
        let mut p = Lrfu::new(1e9); // effectively LFU
        p.touch(1, 0);
        p.touch(1, 1);
        p.touch(2, 2);
        assert_eq!(p.victim(&|_| false), Some(2));
        let mut q = Lrfu::new(1.0); // strong decay: recency wins
        q.touch(1, 0);
        q.touch(1, 1);
        q.touch(2, 10);
        assert_eq!(q.victim(&|_| false), Some(1));
    }
}
