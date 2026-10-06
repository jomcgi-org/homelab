//! Host expert throughput at Qwen 3.8 Flash record geometry (hidden 2560, inter 640):
//! `cargo run --release -p oominf-cpu --example expert_bench`.

use std::time::Instant;

use oominf_core::Nvfp4Record;
use oominf_cpu::{Geometry, HostExperts, Job};

fn main() -> anyhow::Result<()> {
    let (hidden, inter) = (2560usize, 640usize);
    let half = inter * hidden / 2;
    let sc = inter * hidden / 16;
    let geo = Nvfp4Record {
        hidden,
        inter,
        gate_weight: 256,
        gate_scale: 256 + half,
        up_weight: 256 + half + sc,
        up_scale: 256 + 2 * half + sc,
        down_weight: 256 + 2 * half + 2 * sc,
        down_scale: 256 + 3 * half + 2 * sc,
        scale2: [0, 2, 4],
    };
    let len = 256 + 3 * half + 3 * sc;
    let n_records = 64;
    let mut records: Vec<Vec<u8>> = (0..n_records)
        .map(|i| {
            (0..len)
                .map(|b| ((b * 31 + i * 7) % 251) as u8 & 0x77)
                .collect()
        })
        .collect();
    for r in &mut records {
        for i in [0, 2, 4] {
            r[i * 4..i * 4 + 4].copy_from_slice(&0.01f32.to_le_bytes());
        }
    }
    let x: Vec<f32> = (0..256 * hidden).map(|i| (i % 13) as f32 * 0.01).collect();
    let flops_per_token = 2.0 * 3.0 * (hidden * inter) as f64;
    let threads: Vec<usize> = std::env::args()
        .nth(1)
        .map(|a| a.split(',').map(|v| v.parse().unwrap()).collect())
        .unwrap_or(vec![1, 8, 16]);
    for threads in threads {
        let pool = HostExperts::new(threads)?;
        for (experts, tokens) in [
            (4, 1),
            (2, 2),
            (32, 16),
            (32, 8),
            (32, 16),
            (32, 32),
            (32, 64),
            (16, 128),
            (8, 256),
        ] {
            // Best of five timings of `reps` steps each.
            let reps = 4;
            let mut per = f64::MAX;
            for round in 0..5 {
                let t0 = Instant::now();
                for rep in 0..reps {
                    let jobs = (0..experts)
                        .map(|e| {
                            let r = &records[((round * reps + rep) * experts + e) % n_records];
                            Job {
                                record: r.as_ptr() as usize,
                                len: r.len(),
                                tokens: (0..tokens).collect(),
                            }
                        })
                        .collect();
                    // SAFETY: the records outlive the wait.
                    let mut input = pool.buffer(x.len());
                    input.copy_from_slice(&x);
                    let y = unsafe { pool.submit(Geometry::Nvfp4(geo), input, jobs) }.wait()?;
                    pool.recycle(y);
                }
                per = per.min(t0.elapsed().as_secs_f64() / reps as f64);
            }
            let gbs = (experts * len) as f64 / per / 1e9;
            let gflops = flops_per_token * (experts * tokens) as f64 / per / 1e9;
            println!(
                "threads {threads:2} experts {experts:2} tokens {tokens:3}: {:8.1} us/step, {:7.1} us/expert, {gbs:5.1} GB/s, {gflops:6.1} GFLOP/s",
                per * 1e6,
                per * 1e6 / experts as f64
            );
        }
    }
    Ok(())
}
