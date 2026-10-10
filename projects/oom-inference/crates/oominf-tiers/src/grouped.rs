//! Several expert sources behind one: each expert group (layer) is served by the
//! source for its record layout, so groups with different record sizes get their
//! own arenas.

use std::sync::Arc;

use anyhow::{Context, Result};
use oominf_core::{Backend, ExpertSource, ExpertStats, ExpertTiers, Staged};
use oominf_format::Model;

use crate::TieredExperts;
use crate::host::IoConfig;
use crate::policy::Policy;
use crate::tiered::{CHUNK_SLOTS, TierSizes, min_host_slots};

pub struct GroupedExperts<B> {
    sources: Vec<Box<dyn ExpertSource<B>>>,
    /// `route[layer]`: index into `sources`.
    route: Vec<usize>,
    /// Source whose two-phase fetch is open.
    open: Option<usize>,
    /// Source whose stage-ahead reads may still be in flight.
    staging: Option<usize>,
}

impl<B> GroupedExperts<B> {
    /// `sources[i]` serves every layer `l` with `route[l] == i`.
    pub fn new(sources: Vec<Box<dyn ExpertSource<B>>>, route: Vec<usize>) -> Self {
        GroupedExperts {
            sources,
            route,
            open: None,
            staging: None,
        }
    }

    fn source(&mut self, layer: u32) -> Result<(usize, &mut Box<dyn ExpertSource<B>>)> {
        let i = self
            .route
            .get(layer as usize)
            .copied()
            .filter(|&i| i < self.sources.len())
            .with_context(|| format!("no expert source for group {layer}"))?;
        Ok((i, &mut self.sources[i]))
    }
}

impl<B> ExpertSource<B> for GroupedExperts<B> {
    fn fetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<Vec<u64>> {
        self.source(layer)?.1.fetch(b, layer, experts)
    }

    fn begin_fetch(&mut self, b: &B, layer: u32, experts: &[u32], host_ok: bool) -> Result<Staged> {
        let (i, s) = self.source(layer)?;
        let staged = s.begin_fetch(b, layer, experts, host_ok)?;
        self.open = Some(i);
        Ok(staged)
    }

    fn finish_fetch(&mut self, b: &B) -> Result<()> {
        match self.open.take() {
            Some(i) => self.sources[i].finish_fetch(b),
            None => Ok(()),
        }
    }

    fn prefetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<()> {
        self.source(layer)?.1.prefetch(b, layer, experts)
    }

    fn wants_prefetch(&self) -> bool {
        self.sources.iter().any(|s| s.wants_prefetch())
    }

    fn stage_ahead(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<()> {
        let (i, s) = self.source(layer)?;
        s.stage_ahead(b, layer, experts)?;
        self.staging = Some(i);
        Ok(())
    }

    fn stage_more(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<()> {
        match self.staging {
            Some(i) if self.route.get(layer as usize) == Some(&i) => {
                self.sources[i].stage_more(b, layer, experts)
            }
            _ => Ok(()),
        }
    }

    fn finish_stage_ahead(&mut self, b: &B) -> Result<()> {
        // Polled repeatedly while a stage-ahead's reads land; the staging source
        // finishes it at its own next fetch.
        match self.staging {
            Some(i) => self.sources[i].finish_stage_ahead(b),
            None => Ok(()),
        }
    }

    fn stages_ahead(&self) -> bool {
        self.sources.iter().any(|s| s.stages_ahead())
    }

    fn release_vram(&mut self, b: &B, bytes: usize) -> Result<usize> {
        let mut freed = 0;
        for s in &mut self.sources {
            if freed >= bytes {
                break;
            }
            freed += s.release_vram(b, bytes - freed)?;
        }
        Ok(freed)
    }

    fn releasable_vram(&self) -> usize {
        self.sources.iter().map(|s| s.releasable_vram()).sum()
    }

    fn reclaim_vram(&mut self, b: &B, bytes: usize) -> Result<usize> {
        let mut taken = 0;
        for s in &mut self.sources {
            taken += s.reclaim_vram(b, bytes - taken)?;
        }
        Ok(taken)
    }

    fn stats(&self) -> ExpertStats {
        self.sources
            .iter()
            .map(|s| s.stats())
            .fold(ExpertStats::default(), |a, s| a + s)
    }

    fn tiers(&self) -> ExpertTiers {
        self.sources
            .iter()
            .map(|s| s.tiers())
            .fold(ExpertTiers::default(), |a, t| a + t)
    }

    fn describe(&self) -> String {
        self.sources
            .iter()
            .map(|s| s.describe())
            .collect::<Vec<_>>()
            .join("; ")
    }
}

/// Makes the (VRAM, host) placement policies of one tiered source.
pub type PolicyPair<'a> = dyn Fn() -> Result<(Box<dyn Policy>, Box<dyn Policy>)> + 'a;

/// Share of the host budget offered to each record layout other than the largest
/// (capped at what that layout's records need in total). On the device such a
/// layout gets the smallest tier, one arena chunk: measured on the MTP layer's
/// experts, more device slots barely shorten drafting while every slot taken from
/// the main layout costs decode hits.
const MINOR_HOST_SHARE: f64 = 0.15;

/// Memory a model's tiered sources may take, from [`crate::plan`] or explicit
/// sizes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TierBudget {
    /// VRAM for every layout's tiers, the stage included.
    pub vram_gib: f64,
    /// Pinned host memory for every layout's tiers, the host staging ring
    /// included.
    pub host_gib: f64,
    /// Host staging ring slots of the largest layout (`None`: one layer's worth).
    pub host_stage_slots: Option<usize>,
}

/// Tiered sources for every record layout of `model` within `budget`: one
/// [`TieredExperts`] per layout, behind a [`GroupedExperts`] when there is more
/// than one. `policies()` yields the (VRAM, host) policies of each. No tier is
/// sized beyond the records its layout has: memory past that would never hold
/// anything.
#[allow(clippy::too_many_arguments)]
pub fn tiered_for_model<B: Backend + 'static>(
    b: &Arc<B>,
    model: &Arc<Model>,
    budget: &TierBudget,
    lookahead: bool,
    host_compute: usize,
    policies: &PolicyPair<'_>,
    io: &IoConfig,
) -> Result<Box<dyn ExpertSource<B>>> {
    let groups = &model.index().expert_groups;
    let layouts = layouts(model)?;
    let sizes = split(&layouts, budget);
    let largest = crate::plan::largest(&layouts);

    let mut sources: Vec<Box<dyn ExpertSource<B>>> = Vec::new();
    for (i, (l, sizes)) in layouts.iter().zip(&sizes).enumerate() {
        let (vp, hp) = policies()?;
        let mut t = TieredExperts::new(b.clone(), model.clone(), &l.name, *sizes, vp, hp, io)?;
        t.lookahead = lookahead;
        t.host_compute = host_compute;
        // No prompt warms a small group's records (e.g. a draft head's experts): read
        // them into the host tier now if it can hold them all.
        if i != largest {
            t.preload_host()?;
        }
        sources.push(Box::new(t));
    }
    if sources.len() == 1 {
        return Ok(sources.pop().expect("one source"));
    }
    let max_layer = groups.iter().map(|g| g.layer).max().unwrap_or(0) as usize;
    let mut route = vec![usize::MAX; max_layer + 1];
    for g in groups {
        route[g.layer as usize] = layouts
            .iter()
            .position(|l| l.name == g.schema.layout)
            .expect("layout listed");
    }
    Ok(Box::new(GroupedExperts::new(sources, route)))
}

const GIB: f64 = (1u64 << 30) as f64;

/// Splits `budget` into each layout's tier sizes. A layout other than the largest
/// gets one VRAM chunk and [`MINOR_HOST_SHARE`] of the host budget (at least its
/// minimum, at most its records). The largest gets the rest; its prefill staging
/// ring (only beside a VRAM stage) comes out of its host share and shrinks, down
/// to one slot, before its host tier would fall below the minimum.
pub fn split(layouts: &[Layout], budget: &TierBudget) -> Vec<TierSizes> {
    let largest = crate::plan::largest(layouts);
    let (mut vram_left, mut host_left) = (budget.vram_gib, budget.host_gib);
    let mut sizes = vec![
        TierSizes {
            vram_slots: 0,
            host_slots: 0,
            host_stage_slots: 1,
            max_fetch: 0,
        };
        layouts.len()
    ];
    for (i, l) in layouts.iter().enumerate() {
        if i != largest {
            // Only decode-sized steps fetch these, at most a chunk's worth at once.
            let max_fetch = CHUNK_SLOTS.min(l.num_experts);
            let vram = CHUNK_SLOTS.min(l.records);
            let share = (budget.host_gib * MINOR_HOST_SHARE * GIB) as usize / l.stride;
            let host = share
                .max(min_host_slots(l.num_experts, max_fetch, false))
                .min(l.records);
            vram_left -= (vram * l.stride) as f64 / GIB;
            host_left -= ((host + 1) * l.stride) as f64 / GIB;
            sizes[i] = TierSizes {
                vram_slots: vram,
                host_slots: host,
                host_stage_slots: 1,
                max_fetch,
            };
        }
    }
    let l = &layouts[largest];
    let vram = ((vram_left.max(0.0) * GIB) as usize / l.stride).min(l.records);
    let slots = (host_left.max(0.0) * GIB) as usize / l.stride;
    // The ring exists only beside a VRAM stage (see `TieredExperts::new`).
    let stage = vram >= 2 * l.num_experts;
    let ring = if stage {
        let min = min_host_slots(l.num_experts, l.num_experts, true);
        budget
            .host_stage_slots
            .unwrap_or(l.num_experts)
            .min(slots.saturating_sub(min))
            .clamp(1, l.num_experts)
    } else {
        1
    };
    sizes[largest] = TierSizes {
        vram_slots: vram,
        host_slots: slots.saturating_sub(ring).min(l.records),
        host_stage_slots: ring,
        max_fetch: l.num_experts,
    };
    sizes
}

/// One record layout of a model: its records, all of one size.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Layout {
    pub name: String,
    /// Bytes per record.
    pub stride: usize,
    /// Experts per layer.
    pub num_experts: usize,
    /// Records of this layout in the whole model.
    pub records: usize,
}

/// The record layouts of `model`, in order of first appearance.
pub fn layouts(model: &Model) -> Result<Vec<Layout>> {
    let mut out: Vec<Layout> = Vec::new();
    for g in &model.index().expert_groups {
        match out.iter_mut().find(|l| l.name == g.schema.layout) {
            Some(l) => l.records += g.num_experts as usize,
            None => out.push(Layout {
                name: g.schema.layout.clone(),
                stride: g.schema.stride as usize,
                num_experts: g.num_experts as usize,
                records: g.num_experts as usize,
            }),
        }
    }
    anyhow::ensure!(!out.is_empty(), "model has no expert groups");
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const GIB: f64 = (1u64 << 30) as f64;

    fn qwen() -> Vec<Layout> {
        vec![
            Layout {
                name: "nvfp4".into(),
                stride: 2768896,
                num_experts: 512,
                records: 512 * 48,
            },
            Layout {
                name: "bf16".into(),
                stride: 9830400,
                num_experts: 512,
                records: 512,
            },
        ]
    }

    fn budget(vram: f64, host: f64) -> TierBudget {
        TierBudget {
            vram_gib: vram,
            host_gib: host,
            host_stage_slots: None,
        }
    }

    #[test]
    fn a_large_budget_gets_a_whole_layer_ring() {
        let s = split(&qwen(), &budget(16.4, 45.2));
        assert_eq!(s[0].host_stage_slots, 512);
        assert_eq!(s[1].host_slots, 512);
        assert!(s[0].host_slots > 14000, "{:?}", s[0]);
    }

    /// The planner's floor (with a one-slot ring) must yield working tiers: the
    /// ring shrinks before the host tier falls below its minimum.
    #[test]
    fn the_floor_budget_shrinks_the_ring_not_the_tier() {
        let l = qwen();
        let floor = crate::plan::floors(&l, true).host as f64 / GIB;
        let s = split(&l, &budget(16.4, floor));
        assert!(
            s[0].host_slots >= min_host_slots(512, 512, true),
            "{:?}",
            s[0]
        );
        assert_eq!(s[0].host_stage_slots, 1);
        assert!(s[1].host_slots >= min_host_slots(512, 64, false));
        // A little more goes to the ring first.
        let s = split(&l, &budget(16.4, floor + 0.5));
        assert!(s[0].host_stage_slots > 100, "{:?}", s[0]);
    }
}
