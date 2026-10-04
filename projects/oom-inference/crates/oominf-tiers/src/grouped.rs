//! Several expert sources behind one: each expert group (layer) is served by the
//! source for its record layout, so groups with different record sizes get their
//! own arenas.

use std::sync::Arc;

use anyhow::{Context, Result};
use oominf_core::{Backend, ExpertSource, ExpertStats, Staged};
use oominf_format::Model;

use crate::policy::Policy;
use crate::tiered::CHUNK_SLOTS;
use crate::{TieredExperts, slots_for};

pub struct GroupedExperts<B> {
    sources: Vec<Box<dyn ExpertSource<B>>>,
    /// `route[layer]`: index into `sources`.
    route: Vec<usize>,
    /// Source whose two-phase fetch is open.
    open: Option<usize>,
}

impl<B> GroupedExperts<B> {
    /// `sources[i]` serves every layer `l` with `route[l] == i`.
    pub fn new(sources: Vec<Box<dyn ExpertSource<B>>>, route: Vec<usize>) -> Self {
        GroupedExperts {
            sources,
            route,
            open: None,
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

    fn begin_fetch(&mut self, b: &B, layer: u32, experts: &[u32]) -> Result<Staged> {
        let (i, s) = self.source(layer)?;
        let staged = s.begin_fetch(b, layer, experts)?;
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

/// Tiered sources for every record layout of `model` within `vram_gib` and
/// `host_gib`: one [`TieredExperts`] per layout, behind a [`GroupedExperts`] when
/// there is more than one. `policies()` yields the (VRAM, host) policies of each.
pub fn tiered_for_model<B: Backend + 'static>(
    b: &Arc<B>,
    model: &Arc<Model>,
    vram_gib: f64,
    host_gib: f64,
    lookahead: bool,
    policies: &PolicyPair<'_>,
) -> Result<Box<dyn ExpertSource<B>>> {
    const GIB: f64 = (1u64 << 30) as f64;
    let groups = &model.index().expert_groups;
    // Layouts in order of first appearance, with their total record bytes.
    let mut layouts: Vec<(String, f64)> = Vec::new();
    let mut strides: Vec<f64> = Vec::new();
    for g in groups {
        let bytes = g.num_experts as f64 * g.schema.stride as f64 / GIB;
        match layouts.iter_mut().find(|(l, _)| *l == g.schema.layout) {
            Some((_, total)) => *total += bytes,
            None => {
                layouts.push((g.schema.layout.clone(), bytes));
                strides.push(g.schema.stride as f64 / GIB);
            }
        }
    }
    let largest = layouts
        .iter()
        .enumerate()
        .max_by(|a, b| a.1.1.total_cmp(&b.1.1))
        .map(|(i, _)| i)
        .context("model has no expert groups")?;
    let (mut vram_left, mut host_left) = (vram_gib, host_gib);
    let mut budgets = vec![(0.0, 0.0); layouts.len()];
    for (i, (_, total)) in layouts.iter().enumerate() {
        if i != largest {
            budgets[i] = (
                (CHUNK_SLOTS as f64 * strides[i]).min(*total),
                (host_gib * MINOR_HOST_SHARE).min(*total),
            );
            vram_left -= budgets[i].0;
            host_left -= budgets[i].1;
        }
    }
    budgets[largest] = (vram_left, host_left);

    let mut sources: Vec<Box<dyn ExpertSource<B>>> = Vec::new();
    for (i, ((layout, _), (vram, host))) in layouts.iter().zip(&budgets).enumerate() {
        let (vp, hp) = policies()?;
        let mut t = TieredExperts::new(
            b.clone(),
            model.clone(),
            layout,
            slots_for(model, layout, *vram),
            slots_for(model, layout, *host),
            vp,
            hp,
        )?;
        t.lookahead = lookahead;
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
            .position(|(l, _)| *l == g.schema.layout)
            .expect("layout listed");
    }
    Ok(Box::new(GroupedExperts::new(sources, route)))
}
