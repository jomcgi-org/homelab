//! Host-side token sampling from one row of logits.

/// Sampling parameters, already resolved against the model's defaults.
#[derive(Debug, Clone, PartialEq)]
pub struct SamplingParams {
    /// 0 means greedy.
    pub temperature: f32,
    /// Nucleus mass in (0, 1]; 1 disables it.
    pub top_p: f32,
    /// 0 disables top-k.
    pub top_k: usize,
    pub presence_penalty: f32,
    pub frequency_penalty: f32,
    pub seed: Option<u64>,
}

impl Default for SamplingParams {
    fn default() -> Self {
        SamplingParams {
            temperature: 1.0,
            top_p: 1.0,
            top_k: 0,
            presence_penalty: 0.0,
            frequency_penalty: 0.0,
            seed: None,
        }
    }
}

/// SplitMix64: small, fast and good enough for sampling.
struct Rng(u64);

impl Rng {
    fn next_f64(&mut self) -> f64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        (z >> 11) as f64 / (1u64 << 53) as f64
    }
}

pub struct Sampler {
    params: SamplingParams,
    rng: Rng,
    counts: std::collections::HashMap<u32, u32>,
}

impl Sampler {
    pub fn new(params: SamplingParams) -> Self {
        let seed = params.seed.unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos() as u64)
                .unwrap_or(0)
        });
        Sampler {
            params,
            rng: Rng(seed),
            counts: Default::default(),
        }
    }

    /// Picks the next token and records it for the penalties.
    pub fn sample(&mut self, logits: &[f32]) -> u32 {
        self.choose(logits, None)
    }

    /// Picks the next token given `draft`, a token proposed for this position:
    /// greedy decoding keeps the draft exactly when it is the argmax; sampling keeps
    /// it with the target's probability of it and otherwise draws from the target
    /// with the draft excluded, so the result is distributed exactly as
    /// [`Sampler::sample`]'s. Records the token for the penalties.
    pub fn choose(&mut self, logits: &[f32], draft: Option<u32>) -> u32 {
        let p = &self.params;
        let penalised = p.presence_penalty != 0.0 || p.frequency_penalty != 0.0;
        let mut adjusted;
        let logits = if penalised && !self.counts.is_empty() {
            adjusted = logits.to_vec();
            for (&tok, &n) in &self.counts {
                adjusted[tok as usize] -= p.presence_penalty + p.frequency_penalty * n as f32;
            }
            &adjusted[..]
        } else {
            logits
        };
        let id = if p.temperature <= 0.0 {
            argmax(logits)
        } else {
            let (ids, mut probs) = self.distribution(logits);
            let u = self.rng.next_f64();
            match draft.and_then(|d| ids.iter().position(|&i| i == d)) {
                Some(j) if u < probs[j] => ids[j],
                found => {
                    // Rejected (or outside the filtered set): the residual distribution.
                    if let Some(j) = found {
                        probs[j] = 0.0;
                    }
                    self.pick(&ids, &probs)
                }
            }
        };
        *self.counts.entry(id).or_default() += 1;
        id
    }

    /// Token ids and normalised probabilities after temperature, top-k and top-p.
    fn distribution(&self, logits: &[f32]) -> (Vec<u32>, Vec<f64>) {
        let p = &self.params;
        let mut cand: Vec<(u32, f32)> = logits
            .iter()
            .enumerate()
            .map(|(i, &l)| (i as u32, l))
            .collect();
        let k = if p.top_k > 0 {
            p.top_k.min(cand.len())
        } else {
            cand.len()
        };
        if k < cand.len() {
            cand.select_nth_unstable_by(k - 1, |a, b| b.1.total_cmp(&a.1));
            cand.truncate(k);
        }
        cand.sort_unstable_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
        let inv_t = 1.0 / p.temperature as f64;
        let max = cand[0].1 as f64;
        let mut probs: Vec<f64> = cand
            .iter()
            .map(|c| ((c.1 as f64 - max) * inv_t).exp())
            .collect();
        let total: f64 = probs.iter().sum();
        if p.top_p < 1.0 {
            let mut acc = 0.0;
            let cut = probs
                .iter()
                .position(|&q| {
                    acc += q / total;
                    acc >= p.top_p as f64
                })
                .map_or(probs.len(), |i| i + 1);
            probs.truncate(cut);
        }
        let mass: f64 = probs.iter().sum();
        let ids = cand[..probs.len()].iter().map(|c| c.0).collect();
        (ids, probs.into_iter().map(|q| q / mass).collect())
    }

    /// Draws from `probs` (any non-negative weights) over `ids`.
    fn pick(&mut self, ids: &[u32], probs: &[f64]) -> u32 {
        let mass: f64 = probs.iter().sum();
        let mut r = self.rng.next_f64() * mass;
        let mut last = 0;
        for (i, &q) in probs.iter().enumerate() {
            if q > 0.0 {
                last = i;
                r -= q;
                if r <= 0.0 {
                    return ids[i];
                }
            }
        }
        ids[last]
    }
}

use oominf_core::argmax;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn greedy_picks_the_max_and_lowest_id_on_ties() {
        let mut s = Sampler::new(SamplingParams {
            temperature: 0.0,
            ..Default::default()
        });
        assert_eq!(s.sample(&[0.1, 3.0, 3.0, -1.0]), 1);
    }

    #[test]
    fn top_k_one_is_greedy_and_seeds_are_reproducible() {
        let logits: Vec<f32> = (0..100).map(|i| ((i * 37) % 11) as f32).collect();
        let mut s = Sampler::new(SamplingParams {
            top_k: 1,
            seed: Some(1),
            ..Default::default()
        });
        assert_eq!(s.sample(&logits), argmax(&logits));
        let params = SamplingParams {
            temperature: 0.8,
            top_p: 0.9,
            seed: Some(7),
            ..Default::default()
        };
        let a: Vec<u32> = {
            let mut s = Sampler::new(params.clone());
            (0..20).map(|_| s.sample(&logits)).collect()
        };
        let b: Vec<u32> = {
            let mut s = Sampler::new(params);
            (0..20).map(|_| s.sample(&logits)).collect()
        };
        assert_eq!(a, b);
    }

    #[test]
    fn top_p_restricts_to_the_nucleus() {
        // One dominant token holds almost all the mass.
        let mut logits = vec![0.0f32; 50];
        logits[9] = 20.0;
        let mut s = Sampler::new(SamplingParams {
            top_p: 0.5,
            seed: Some(3),
            ..Default::default()
        });
        for _ in 0..50 {
            assert_eq!(s.sample(&logits), 9);
        }
    }

    #[test]
    fn frequency_penalty_discourages_repeats() {
        let logits = [1.0f32, 0.9];
        let mut s = Sampler::new(SamplingParams {
            temperature: 0.0,
            frequency_penalty: 1.0,
            ..Default::default()
        });
        assert_eq!(s.sample(&logits), 0);
        assert_eq!(s.sample(&logits), 1);
    }

    #[test]
    fn choosing_with_a_draft_keeps_the_target_distribution() {
        // Target after temperature 1: softmax([1, 0.5, 0]) = [0.506, 0.307, 0.186].
        let logits = [1.0f32, 0.5, 0.0];
        let z: f64 = logits.iter().map(|&l| (l as f64).exp()).sum();
        let target: Vec<f64> = logits.iter().map(|&l| (l as f64).exp() / z).collect();
        let n = 200_000;
        for draft in [None, Some(0), Some(1), Some(2), Some(7)] {
            let mut s = Sampler::new(SamplingParams {
                seed: Some(42),
                ..Default::default()
            });
            let mut counts = [0usize; 3];
            for _ in 0..n {
                counts[s.choose(&logits, draft) as usize] += 1;
            }
            for (x, &c) in counts.iter().enumerate() {
                let got = c as f64 / n as f64;
                assert!(
                    (got - target[x]).abs() < 0.005,
                    "draft {draft:?}: P({x}) = {got:.4}, target {:.4}",
                    target[x]
                );
            }
        }
    }

    #[test]
    fn greedy_choice_ignores_the_draft() {
        let mut s = Sampler::new(SamplingParams {
            temperature: 0.0,
            ..Default::default()
        });
        assert_eq!(s.choose(&[0.1, 3.0, 2.0], Some(2)), 1);
        assert_eq!(s.choose(&[0.1, 3.0, 2.0], Some(1)), 1);
    }
}
