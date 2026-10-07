//! O_DIRECT read throughput of whole expert records through `DirectReader`, at the
//! sizes prefill staging reads (a layer's misses per batch). Needs a converted model:
//! `OOMINF_MODEL=<model.oom> cargo test --release -p oominf-tiers --test read_throughput -- --ignored --nocapture`.

use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::Result;
use oominf_format::Model;
use oominf_tiers::host::{DirectReader, ReadJob};

/// Page-aligned anonymous memory for `n` bytes (unpinned: disk rate only).
struct Buf(*mut u8, usize);

impl Buf {
    fn new(n: usize) -> Buf {
        let flags = libc::MAP_PRIVATE | libc::MAP_ANONYMOUS;
        #[cfg(target_os = "linux")]
        let flags = flags | libc::MAP_POPULATE;
        // SAFETY: anonymous private mapping, checked below.
        let p = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                n,
                libc::PROT_READ | libc::PROT_WRITE,
                flags,
                -1,
                0,
            )
        };
        assert_ne!(p, libc::MAP_FAILED);
        Buf(p.cast(), n)
    }
}

impl Drop for Buf {
    fn drop(&mut self) {
        // SAFETY: mapped in `new`.
        unsafe { libc::munmap(self.0.cast(), self.1) };
    }
}

/// Reads `records` (file offset, length) split into pieces of at most `piece` bytes
/// across `readers` rings; returns GB/s.
fn read(path: &Path, records: &[(u64, usize)], piece: usize, readers: usize) -> Result<f64> {
    let total: usize = records.iter().map(|r| r.1).sum();
    let buf = Buf::new(total);
    let mut jobs: Vec<Vec<ReadJob>> = (0..readers).map(|_| Vec::new()).collect();
    let mut at = 0usize;
    for (i, &(offset, len)) in records.iter().enumerate() {
        let mut done = 0;
        while done < len {
            let n = piece.min(len - done);
            let r = &mut jobs[i % readers];
            let tag = r.len();
            r.push(ReadJob {
                offset: offset + done as u64,
                // SAFETY: within `buf`.
                dst: unsafe { buf.0.add(at + done) },
                len: n,
                tag,
            });
            done += n;
        }
        at += len;
    }
    let mut rings = (0..readers)
        .map(|_| DirectReader::open(path, 32))
        .collect::<Result<Vec<_>>>()?;
    let t = Instant::now();
    for (ring, j) in rings.iter_mut().zip(jobs) {
        ring.submit(j)?;
    }
    for ring in &mut rings {
        ring.drain(|_| Ok(()))?;
    }
    Ok(total as f64 / t.elapsed().as_secs_f64() / 1e9)
}

#[test]
#[ignore = "needs a converted model on NVMe"]
fn staging_batch_throughput() -> Result<()> {
    let dir = PathBuf::from(std::env::var("OOMINF_MODEL")?);
    let model = Model::open(&dir)?;
    let path = dir.join("experts.bin");
    // A prefill layer's staged misses: about 140 of 512 records. Each configuration
    // reads a different layer, twice, so the drive's own cache never serves a repeat.
    let configs = [
        (usize::MAX, 1),
        (1 << 20, 1),
        (256 << 10, 1),
        (usize::MAX, 2),
        (1 << 20, 2),
        (usize::MAX, 4),
    ];
    for round in 0..2u32 {
        for (i, &(piece, readers)) in configs.iter().enumerate() {
            let layer = 8 + round * 20 + i as u32;
            let records = (0..512u32)
                .step_by(3)
                .take(140)
                .map(|e| {
                    model
                        .record_location(layer, e)
                        .map(|(o, s)| (o, s as usize))
                })
                .collect::<oominf_format::Result<Vec<_>>>()?;
            let mb = records.iter().map(|r| r.1).sum::<usize>() as f64 / 1e6;
            let gbs = read(&path, &records, piece, readers)?;
            let p = match piece {
                usize::MAX => "whole records".to_string(),
                n => format!("{} KiB pieces", n >> 10),
            };
            println!("layer {layer}: {mb:.0} MB, {p}, {readers} ring(s): {gbs:.2} GB/s");
        }
    }
    Ok(())
}
