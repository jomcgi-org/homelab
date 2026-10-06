//! Sizing the expert tiers from fresh checks of the machine: shrink what does not
//! fit (with a warning) and fail only when even the smallest working tiers do
//! not, saying what is missing.
//!
//! Host memory is split as `usable = reserve + outside + tiers`:
//! - `usable` is the smaller of `MemAvailable` and the control group's headroom
//!   ([`crate::resources::HostMemory`]);
//! - `reserve` is left alone (page cache for embedding-table rows, the OS, the
//!   process's own heap); when the tiers' minimum does not fit, a default reserve
//!   is lowered as far as [`MIN_HOST_RESERVE`], an explicit one is kept;
//! - `outside` is host memory the model takes beside the tiers at the configured
//!   context: host-placed KV caches, prefix checkpoints, prefix store snapshots and
//!   small pinned transfer buffers;
//! - the tiers get the rest (or the requested size, shrunk to the rest), capped at
//!   what the model's records fill.
//!
//! VRAM is split the same way from the free memory left after the model and its
//! first sequence are loaded.

use anyhow::{Result, bail};

use crate::grouped::Layout;
use crate::tiered::{CHUNK_SLOTS, min_host_slots, min_vram_slots};

const GIB: f64 = (1u64 << 30) as f64;

/// Host memory left to the process when a default `--host-reserve-gib` is lowered
/// to fit the tiers' minimum: the CUDA context, the tokenizer and work buffers,
/// and a little page cache for embedding-table rows.
pub const MIN_HOST_RESERVE: u64 = 3 << 30;

/// VRAM left free when a default `--vram-reserve-gib` is lowered to fit the
/// tiers' minimum: decode workspaces and a short prefill's activations.
pub const MIN_VRAM_RESERVE: u64 = 1 << 30;

fn gib(b: u64) -> String {
    format!("{:.1} GiB", b as f64 / GIB)
}

/// Bytes of the smallest working tiers of `layouts`, and of the tiers that would
/// hold every record (more is never used). `stage`: the largest layout's VRAM tier
/// has room for a stage (see `TieredExperts::new`).
pub fn floors(layouts: &[Layout], stage: bool) -> Floors {
    let largest = largest(layouts);
    let mut f = Floors::default();
    for (i, l) in layouts.iter().enumerate() {
        let main = i == largest;
        let max_fetch = if main {
            l.num_experts
        } else {
            CHUNK_SLOTS.min(l.num_experts)
        };
        let stage = main && stage;
        let ring = 1;
        f.vram += (min_vram_slots(l.num_experts, max_fetch, stage)
            + if stage { l.num_experts } else { 0 }) as u64
            * l.stride as u64;
        f.host += ((min_host_slots(l.num_experts, max_fetch, stage) + ring) * l.stride) as u64;
        f.host_useful += ((l.records + if main { l.num_experts } else { ring }) * l.stride) as u64;
        f.vram_useful += ((l.records + if main { l.num_experts } else { 0 }) * l.stride) as u64;
    }
    f
}

/// Index of the layout with the most record bytes.
pub fn largest(layouts: &[Layout]) -> usize {
    layouts
        .iter()
        .enumerate()
        .max_by_key(|(_, l)| l.records * l.stride)
        .map_or(0, |(i, _)| i)
}

/// VRAM a stage needs beside the largest layout's main tier (two layers' worth).
pub fn stage_bytes(layouts: &[Layout]) -> u64 {
    let l = &layouts[largest(layouts)];
    (2 * l.num_experts * l.stride) as u64
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Floors {
    /// Smallest working tiers.
    pub host: u64,
    pub vram: u64,
    /// Tiers holding every record (and a full staging ring).
    pub host_useful: u64,
    pub vram_useful: u64,
}

/// What one memory's split starts from.
#[derive(Debug, Clone)]
pub struct Request {
    /// Fresh figure: usable host memory, or free VRAM.
    pub usable: u64,
    /// Left alone for everything else.
    pub reserve: u64,
    /// The reserve was set explicitly (never lowered then).
    pub reserve_explicit: bool,
    /// Lowest the reserve may go when it was not explicit.
    pub min_reserve: u64,
    /// An explicit tier size (`--host-expert-gib`, `--vram-expert-gib`).
    pub requested: Option<u64>,
    /// Memory the model takes beside the tiers, by what takes it.
    pub outside: Vec<(String, u64)>,
    /// Smallest working tiers, and tiers that hold every record.
    pub floor: u64,
    pub useful: u64,
}

/// The tiers' size, and warnings for everything shrunk to get it.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub bytes: u64,
    pub reserve: u64,
    pub warnings: Vec<String>,
}

/// Splits one memory (`what`: "host memory" or "VRAM") as the module describes.
pub fn plan(what: &str, r: &Request) -> Result<Plan> {
    let outside: u64 = r.outside.iter().map(|(_, b)| b).sum();
    let mut warnings = Vec::new();
    let mut reserve = r.reserve;
    let room = |reserve: u64| r.usable.saturating_sub(reserve + outside);
    let mut bytes = match r.requested {
        Some(want) if want > room(reserve) => {
            warnings.push(format!(
                "{what}: requested {} for the expert tiers but only {} fits ({} usable - {} reserve - {} outside the tiers); using {}",
                gib(want),
                gib(room(reserve)),
                gib(r.usable),
                gib(reserve),
                gib(outside),
                gib(room(reserve))
            ));
            room(reserve)
        }
        Some(want) => want,
        None => room(reserve),
    };
    if bytes > r.useful {
        if r.requested.is_some() {
            warnings.push(format!(
                "{what}: the expert tiers need at most {} to hold every record; capped there",
                gib(r.useful)
            ));
        }
        bytes = r.useful;
    }
    if bytes < r.floor && !r.reserve_explicit && room(r.min_reserve) >= r.floor {
        let lowered = r.usable - outside - r.floor;
        warnings.push(format!(
            "{what}: lowered the reserve from {} to {} to fit the smallest working expert tiers ({})",
            gib(reserve),
            gib(lowered),
            gib(r.floor)
        ));
        reserve = lowered;
        bytes = r.floor;
    }
    if bytes < r.floor {
        let mut need = format!(
            "{what}: the smallest working expert tiers need {}",
            gib(r.floor)
        );
        for (name, b) in &r.outside {
            need += &format!(" + {} for {name}", gib(*b));
        }
        let min_reserve = if r.reserve_explicit {
            r.reserve
        } else {
            r.min_reserve
        };
        need += &format!(
            " + {} reserve = {}, but only {} is usable",
            gib(min_reserve),
            gib(r.floor + outside + min_reserve),
            gib(r.usable)
        );
        bail!(need);
    }
    Ok(Plan {
        bytes,
        reserve,
        warnings,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const G: u64 = 1 << 30;

    fn req(usable: u64) -> Request {
        Request {
            usable,
            reserve: 12 * G,
            reserve_explicit: false,
            min_reserve: 3 * G,
            requested: None,
            outside: vec![("prefix checkpoints".into(), G)],
            floor: 2 * G,
            useful: 70 * G,
        }
    }

    #[test]
    fn default_budget_is_the_rest_capped_at_every_record() {
        let p = plan("host memory", &req(60 * G)).unwrap();
        assert_eq!((p.bytes, p.warnings.len()), (47 * G, 0));
        let p = plan("host memory", &req(200 * G)).unwrap();
        assert_eq!((p.bytes, p.warnings.len()), (70 * G, 0));
    }

    #[test]
    fn a_request_that_does_not_fit_shrinks_with_a_warning() {
        let mut r = req(32 * G);
        r.requested = Some(40 * G);
        let p = plan("host memory", &r).unwrap();
        assert_eq!(p.bytes, 19 * G);
        assert!(
            p.warnings[0].contains("requested 40.0 GiB"),
            "{:?}",
            p.warnings
        );
        r.requested = Some(100 * G);
        r.usable = 500 * G;
        let p = plan("host memory", &r).unwrap();
        assert_eq!(p.bytes, 70 * G);
        assert!(p.warnings[0].contains("capped"));
    }

    #[test]
    fn a_default_reserve_is_lowered_to_fit_the_floor() {
        // 10 GiB usable: 12 GiB reserve leaves nothing; 3 GiB would leave 6.
        let p = plan("host memory", &req(10 * G)).unwrap();
        assert_eq!((p.bytes, p.reserve), (2 * G, 7 * G));
        assert!(p.warnings[0].contains("lowered the reserve"));
    }

    #[test]
    fn too_little_memory_fails_saying_what_is_missing() {
        let e = plan("host memory", &req(5 * G)).unwrap_err().to_string();
        assert!(e.contains("2.0 GiB"), "{e}");
        assert!(e.contains("1.0 GiB for prefix checkpoints"), "{e}");
        assert!(e.contains("only 5.0 GiB is usable"), "{e}");
        // An explicit reserve is kept.
        let mut r = req(10 * G);
        r.reserve_explicit = true;
        assert!(plan("host memory", &r).is_err());
    }

    #[test]
    fn floors_follow_the_stage_and_minor_layouts() {
        let l = [
            Layout {
                name: "main".into(),
                stride: 4096,
                num_experts: 512,
                records: 512 * 48,
            },
            Layout {
                name: "minor".into(),
                stride: 8192,
                num_experts: 512,
                records: 512,
            },
        ];
        let f = floors(&l, true);
        // Main: one chunk + a 512-record stage of VRAM, 64 + 1 host slots; minor:
        // one chunk of VRAM, 64 + 1 host slots.
        assert_eq!(f.vram, (64 + 512) * 4096 + 64 * 8192);
        assert_eq!(f.host, 65 * 4096 + 65 * 8192);
        let f = floors(&l, false);
        assert_eq!(f.vram, 512 * 4096 + 64 * 8192);
        assert_eq!(f.host, 513 * 4096 + 65 * 8192);
        assert_eq!(stage_bytes(&l), 2 * 512 * 4096);
    }
}
