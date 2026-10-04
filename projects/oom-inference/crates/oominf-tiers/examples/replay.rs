//! Replays a recorded routing trace through the real tier placement code (no GPU):
//! VRAM hit rate per policy and size, and disk reads per token behind a host tier.
//!
//!     cargo run --release -p oominf-tiers --example replay -- <trace.u16>

use std::collections::HashSet;

use oominf_tiers::cache::{Place, SlotCache};
use oominf_tiers::{load_trace, policy};

const LAYERS: usize = 48;
const TOP_K: usize = 10;
const EXPERTS: u32 = 512;
const WARMUP: usize = 256;

/// Returns (VRAM misses, disk reads) per token after warmup.
fn replay(
    trace: &[Vec<Vec<u16>>],
    vram: &mut SlotCache,
    host: Option<&mut SlotCache>,
) -> (f64, f64) {
    let mut host = host;
    let (mut vmiss, mut dmiss, mut tokens) = (0u64, 0u64, 0u64);
    for (step, layers) in trace.iter().enumerate() {
        let count = step >= WARMUP;
        tokens += u64::from(count);
        for (l, experts) in layers.iter().enumerate() {
            let keys: HashSet<u32> = experts
                .iter()
                .map(|&e| l as u32 * EXPERTS + e as u32)
                .collect();
            let pinned = |k: u32| keys.contains(&k);
            for &k in &keys {
                if let Some(Place::Miss(..)) = vram.place(k, &pinned) {
                    vmiss += u64::from(count);
                    if let Some(h) = host.as_deref_mut()
                        && let Some(Place::Miss(..)) = h.place(k, &pinned)
                    {
                        dmiss += u64::from(count);
                    }
                }
            }
        }
    }
    (vmiss as f64 / tokens as f64, dmiss as f64 / tokens as f64)
}

fn main() -> anyhow::Result<()> {
    let path = std::env::args().nth(1).expect("trace path");
    let trace = load_trace(path.as_ref(), LAYERS, TOP_K)?;
    println!(
        "trace: {} steps; stats after {WARMUP} warmup steps; 480 expert requests per token",
        trace.len()
    );
    let tok = (LAYERS * TOP_K) as f64; // accesses per token
    let policies: Vec<String> = ["lru".to_string()]
        .into_iter()
        .chain(
            [4.0, 16.0, 64.0, 256.0, 1024.0]
                .iter()
                .map(|h| format!("lrfu:{}", h * tok)),
        )
        .chain(["lfu".to_string()])
        .collect();
    println!("\nVRAM tier alone: misses per token (slots per layer x 48 layers)");
    print!("{:<22}", "policy");
    let sizes = [32usize, 64, 96, 128, 192, 256];
    for s in sizes {
        print!("{:>9}", format!("{s}/L"));
    }
    println!();
    for p in &policies {
        print!("{:<22}", p);
        for s in sizes {
            let mut c = SlotCache::new(s * LAYERS, policy::parse(p)?);
            print!("{:>9.1}", replay(&trace, &mut c, None).0);
        }
        println!();
    }
    println!("\nTwo tiers: disk reads per token, VRAM 48/L (about 6 GiB) over host N/L");
    print!("{:<22}", "policy (both tiers)");
    let hsizes = [96usize, 128, 192, 256, 320];
    for s in hsizes {
        print!("{:>9}", format!("{s}/L"));
    }
    println!();
    for p in &policies {
        print!("{:<22}", p);
        for s in hsizes {
            let mut v = SlotCache::new(48 * LAYERS, policy::parse(p)?);
            let mut h = SlotCache::new(s * LAYERS, policy::parse(p)?);
            print!("{:>9.2}", replay(&trace, &mut v, Some(&mut h)).1);
        }
        println!();
    }
    Ok(())
}
