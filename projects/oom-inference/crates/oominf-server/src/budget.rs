//! The per-step token budget of continuous batching: which draft tokens ride in a
//! step beside every active stream's next token.
//!
//! A step's cost grows with its width (the tokens it carries) much more slowly
//! than linearly, because routed-expert fetches are shared by every row of a step
//! (see `docs/ARCHITECTURE.md`). Each step carries every decoding stream's next
//! token (worth one token each) and the drafts whose expected value pays for the
//! width they add: draft `j` of a stream is accepted with about `a^j`, where `a`
//! is that stream's recent per-draft acceptance rate. The allocator picks the set
//! that maximises expected tokens per millisecond under the [`CostCurve`].

use anyhow::{Context, Result, ensure};

/// Milliseconds a step costs by width (tokens it carries): configured points,
/// interpolated linearly, refined online by measured steps.
#[derive(Debug, Clone)]
pub struct CostCurve {
    /// `ms[w]` for widths `1..ms.len()` (`ms[0]` unused).
    ms: Vec<f64>,
}

/// Measured on an RTX 4090 with warm tiers (max-perf config, verify steps of
/// width w, #6872): 1-2 tokens 25-50 ms, 4 about 70, 8 about 110-125, 16 about 210.
pub const DEFAULT_STEP_COST: &str = "1:30,2:45,4:70,8:118,16:210";

/// Weight of a new measurement in a width's running cost.
const COST_EMA: f64 = 0.2;

impl CostCurve {
    /// Parses `width:ms` points separated by commas, e.g. `1:30,4:70,16:210`, for
    /// widths up to `max_width` (beyond the last point the last segment's slope
    /// continues).
    pub fn parse(s: &str, max_width: usize) -> Result<Self> {
        let mut pts: Vec<(usize, f64)> = s
            .split(',')
            .map(|p| {
                let (w, ms) = p
                    .split_once(':')
                    .context("step cost point is not width:ms")?;
                Ok((w.trim().parse()?, ms.trim().parse()?))
            })
            .collect::<Result<_>>()?;
        pts.sort_by_key(|p| p.0);
        ensure!(
            !pts.is_empty() && pts[0].0 >= 1 && pts.iter().all(|p| p.1 > 0.0),
            "step cost needs positive widths and costs"
        );
        let at = |w: usize| -> f64 {
            let i = pts.partition_point(|p| p.0 <= w);
            let (lo, hi) = match i {
                0 => (pts[0], *pts.get(1).unwrap_or(&pts[0])),
                i if i >= pts.len() => (pts[pts.len().saturating_sub(2)], pts[pts.len() - 1]),
                i => (pts[i - 1], pts[i]),
            };
            if hi.0 == lo.0 {
                return lo.1 * w as f64 / lo.0 as f64;
            }
            let f = (w as f64 - lo.0 as f64) / (hi.0 as f64 - lo.0 as f64);
            (lo.1 + f * (hi.1 - lo.1)).max(1e-3)
        };
        Ok(CostCurve {
            ms: (0..=max_width.max(1)).map(at).collect(),
        })
    }

    /// The widest step the curve covers.
    pub fn max_width(&self) -> usize {
        self.ms.len() - 1
    }

    /// Milliseconds a step of `width` tokens costs.
    pub fn cost(&self, width: usize) -> f64 {
        self.ms[width.clamp(1, self.max_width())]
    }

    /// Folds in a measured step of `width` tokens that took `ms`.
    pub fn observe(&mut self, width: usize, ms: f64) {
        if (1..=self.max_width()).contains(&width) && ms.is_finite() && ms > 0.0 {
            let c = &mut self.ms[width];
            *c += COST_EMA * (ms - *c);
        }
    }

    /// `width:ms` for every width, for logs.
    pub fn describe(&self) -> String {
        (1..=self.max_width())
            .map(|w| format!("{w}:{:.0}", self.ms[w]))
            .collect::<Vec<_>>()
            .join(",")
    }
}

/// A draft source's running acceptance: the probability that a draft is accepted
/// given that the drafts before it were, estimated with decay.
#[derive(Debug, Clone, Copy)]
pub struct Acceptance {
    hits: f64,
    trials: f64,
}

/// Prior acceptance of a stream's drafts before it has any (MTP measured 55-75%).
const PRIOR_RATE: f64 = 0.6;
/// The prior counts as this many trials.
const PRIOR_TRIALS: f64 = 4.0;
/// Each step's evidence decays older evidence by this factor.
const DECAY: f64 = 0.95;

impl Default for Acceptance {
    fn default() -> Self {
        Acceptance {
            hits: PRIOR_RATE * PRIOR_TRIALS,
            trials: PRIOR_TRIALS,
        }
    }
}

impl Acceptance {
    /// Records a step that verified `drafted` drafts and accepted the first
    /// `accepted`: each accepted draft is a hit, the first rejected one a miss.
    pub fn record(&mut self, drafted: usize, accepted: usize) {
        if drafted == 0 {
            return;
        }
        let trials = (accepted + 1).min(drafted);
        self.hits = self.hits * DECAY + accepted as f64;
        self.trials = self.trials * DECAY + trials as f64;
    }

    pub fn rate(&self) -> f64 {
        (self.hits / self.trials).clamp(0.0, 1.0)
    }
}

/// What one decoding stream can add to a step beyond its next token.
#[derive(Debug, Clone, Copy)]
pub struct Offer {
    /// Draft tokens available at no cost (prompt lookup matched), or 0.
    pub free: usize,
    /// Their acceptance rate.
    pub free_rate: f64,
    /// Most draft tokens the model may draft for it (when `free` is 0).
    pub model: usize,
    pub model_rate: f64,
}

/// The step plan: drafts per stream (in offer order) and the step's width.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub drafts: Vec<usize>,
    /// Whether each stream's drafts are its free ones (else the model's).
    pub free: Vec<bool>,
    pub width: usize,
    /// Expected tokens the step produces.
    pub expected: f64,
}

/// Picks drafts for one step of every stream in `offers`: each stream's next token,
/// plus the drafts that maximise expected tokens per millisecond, where drafting a
/// model token costs `draft_ms` and the step costs `curve.cost(width)`, and the
/// width stays within `curve.max_width()`.
pub fn allocate(offers: &[Offer], curve: &CostCurve, draft_ms: f64) -> Plan {
    let n = offers.len();
    // (value, stream, model-drafted) of each candidate draft; values fall along a
    // stream's chain, so taking candidates by value keeps every stream's drafts a
    // prefix of its chain.
    let mut cands: Vec<(f64, usize, bool)> = Vec::new();
    for (s, o) in offers.iter().enumerate() {
        let (k, rate, model) = if o.free > 0 {
            (o.free, o.free_rate, false)
        } else {
            (o.model, o.model_rate, true)
        };
        let mut v = 1.0;
        for _ in 0..k {
            v *= rate;
            cands.push((v, s, model));
        }
    }
    cands.sort_by(|a, b| b.0.total_cmp(&a.0));
    let room = curve.max_width().saturating_sub(n);
    let mut best = (n as f64 / curve.cost(n), 0usize);
    let (mut value, mut drafting) = (n as f64, 0usize);
    for (i, &(v, _, model)) in cands.iter().take(room).enumerate() {
        value += v;
        drafting += usize::from(model);
        let rate = value / (curve.cost(n + i + 1) + draft_ms * drafting as f64);
        if rate > best.0 {
            best = (rate, i + 1);
        }
    }
    let mut drafts = vec![0; n];
    let mut expected = n as f64;
    for &(v, s, _) in &cands[..best.1] {
        drafts[s] += 1;
        expected += v;
    }
    Plan {
        width: n + best.1,
        free: offers.iter().map(|o| o.free > 0).collect(),
        drafts,
        expected,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn curve() -> CostCurve {
        CostCurve::parse(DEFAULT_STEP_COST, 16).unwrap()
    }

    #[test]
    fn cost_curve_interpolates_and_learns() {
        let mut c = CostCurve::parse("1:30,4:70,16:210", 20).unwrap();
        assert_eq!(c.cost(1), 30.0);
        assert!((c.cost(2) - 30.0 - 40.0 / 3.0).abs() < 1e-9);
        assert_eq!(c.cost(16), 210.0);
        // Beyond the last point the last slope continues.
        assert!((c.cost(20) - (210.0 + 4.0 * 140.0 / 12.0)).abs() < 1e-9);
        c.observe(4, 170.0);
        assert!((c.cost(4) - 90.0).abs() < 1e-9);
        assert!(CostCurve::parse("x", 4).is_err());
    }

    #[test]
    fn acceptance_counts_conditional_trials() {
        let mut a = Acceptance::default();
        assert!((a.rate() - PRIOR_RATE).abs() < 1e-9);
        for _ in 0..200 {
            // 3 drafts, 1 accepted: hit then miss.
            a.record(3, 1);
        }
        assert!((a.rate() - 0.5).abs() < 0.01, "{}", a.rate());
        for _ in 0..200 {
            a.record(2, 2);
        }
        assert!(a.rate() > 0.95);
    }

    fn model(k: usize, rate: f64) -> Offer {
        Offer {
            free: 0,
            free_rate: 0.0,
            model: k,
            model_rate: rate,
        }
    }

    #[test]
    fn one_stream_spends_the_budget_on_drafts() {
        let p = allocate(&[model(3, 0.75)], &curve(), 2.0);
        assert!(p.drafts[0] >= 1, "{p:?}");
        // Useless drafts are not taken.
        let p = allocate(&[model(3, 0.05)], &curve(), 2.0);
        assert_eq!(p.drafts, [0]);
    }

    #[test]
    fn many_streams_take_next_tokens_before_drafts_and_stay_in_width() {
        let offers = vec![model(4, 0.7); 16];
        let p = allocate(&offers, &curve(), 2.0);
        assert_eq!(p.width, 16);
        assert_eq!(p.drafts.iter().sum::<usize>(), 0);
        let offers = vec![model(4, 0.7); 6];
        let p = allocate(&offers, &curve(), 2.0);
        assert!(p.width <= 16 && p.width >= 6, "{p:?}");
    }

    #[test]
    fn free_drafts_win_over_model_drafts() {
        let free = Offer {
            free: 6,
            free_rate: 0.9,
            model: 2,
            model_rate: 0.6,
        };
        let p = allocate(&[free, model(2, 0.6)], &curve(), 5.0);
        assert!(p.free[0] && !p.free[1]);
        assert!(p.drafts[0] > p.drafts[1], "{p:?}");
    }
}
