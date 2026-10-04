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
    let x: Vec<f32> = (0..4 * hidden).map(|i| (i % 13) as f32 * 0.01).collect();
    for threads in [1, 2, 4, 6, 8, 12, 16] {
        let pool = HostExperts::new(threads)?;
        for (experts, tokens) in [(1, 1), (2, 1), (4, 1), (2, 2)] {
            let reps = 40;
            let t0 = Instant::now();
            for rep in 0..reps {
                let jobs = (0..experts)
                    .map(|e| {
                        let r = &records[(rep * experts + e) % n_records];
                        Job {
                            record: r.as_ptr() as usize,
                            len: r.len(),
                            tokens: (0..tokens).collect(),
                        }
                    })
                    .collect();
                // SAFETY: the records outlive the wait.
                unsafe { pool.submit(Geometry::Nvfp4(geo), x.clone(), jobs) }.wait()?;
            }
            let per = t0.elapsed().as_secs_f64() / reps as f64;
            let gbs = (experts * len) as f64 / per / 1e9;
            println!(
                "threads {threads:2} experts {experts} tokens {tokens}: {:7.1} us/step, {:6.1} us/expert, {gbs:5.1} GB/s",
                per * 1e6,
                per * 1e6 / experts as f64
            );
        }
    }
    Ok(())
}
