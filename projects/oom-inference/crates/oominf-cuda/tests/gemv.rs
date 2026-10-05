//! Decode GEMV (`gemm_bf16` with t <= 4): matches an f64 reference, and its
//! throughput on the model's dense shapes as achieved weight bandwidth. Needs a GPU:
//! `cargo test --release -p oominf-cuda --test gemv -- --ignored --nocapture`.

use anyhow::Result;
use oominf_core::{Linear, Memory};
use oominf_cuda::Gpu;

/// Deterministic values in [-1, 1) (xorshift64*).
fn values(n: usize, mut state: u64) -> Vec<f32> {
    (0..n)
        .map(|_| {
            state ^= state >> 12;
            state ^= state << 25;
            state ^= state >> 27;
            ((state.wrapping_mul(0x2545_f491_4f6c_dd1d) >> 40) as f32 / (1u64 << 24) as f32) * 2.0
                - 1.0
        })
        .collect()
}

fn bf16_bits(v: f32) -> u16 {
    // Round to nearest even.
    let b = v.to_bits();
    ((b + 0x7fff + ((b >> 16) & 1)) >> 16) as u16
}

fn bf16_value(b: u16) -> f32 {
    f32::from_bits((b as u32) << 16)
}

/// (n, k) of the dense projections a decode step runs.
const SHAPES: [(usize, usize); 9] = [
    (12288, 2560), // attention q_proj
    (10240, 2560), // GDN in_proj_qkv
    (6144, 2560),  // GDN in_proj_z
    (2560, 6144),  // GDN out_proj, attention o_proj
    (320, 10240),  // hyper-connection mix down
    (10240, 320),  // hyper-connection mix up
    (640, 2560),   // shared expert gate / up, indexer
    (512, 2560),   // router, k / v
    (2560, 640),   // shared expert down
];

#[test]
#[ignore = "needs a GPU"]
fn gemv_matches_reference() -> Result<()> {
    let gpu = Gpu::new(0)?;
    for t in [1, 2, 4] {
        for &(n, k) in &[(640, 2560), (2560, 640), (10240, 320), (300, 10240)] {
            let x = values(t * k, 1 + t as u64);
            let w: Vec<u16> = values(n * k, 7).into_iter().map(bf16_bits).collect();
            let mut y = gpu.zeros(t * n)?;
            let mut scratch = gpu.uninit_bf16(t * k)?;
            gpu.gemm_bf16(
                &gpu.upload_f32(&x)?,
                &gpu.upload_bf16(&w)?,
                &mut y,
                &mut scratch,
                t,
                n,
                k,
            )?;
            let got = gpu.download_f32(&y)?;
            let mut worst = 0f64;
            for r in 0..t {
                for c in 0..n {
                    let want: f64 = (0..k)
                        .map(|i| x[r * k + i] as f64 * bf16_value(w[c * k + i]) as f64)
                        .sum();
                    worst = worst.max((got[r * n + c] as f64 - want).abs() / (k as f64).sqrt());
                }
            }
            assert!(worst < 1e-5, "t={t} {n}x{k}: scaled error {worst:.2e}");
        }
    }
    Ok(())
}

#[test]
#[ignore = "needs a GPU"]
fn gemv_throughput() -> Result<()> {
    let gpu = Gpu::new(0)?;
    let mut total_bytes = 0f64;
    let mut total_s = 0f64;
    for &(n, k) in &SHAPES {
        let t = 2;
        let x = gpu.upload_f32(&values(t * k, 3))?;
        // Rotate over copies totalling well past the 72 MB L2, so every launch
        // reads its weights from DRAM as decode does.
        let copies = (512usize << 20).div_ceil(n * k * 2).max(2);
        let ws: Vec<_> = (0..copies)
            .map(|_| gpu.upload_bf16(&vec![0x3f80u16; n * k]))
            .collect::<Result<_>>()?;
        let mut y = gpu.zeros(t * n)?;
        let mut scratch = gpu.uninit_bf16(t * k)?;
        let reps = 50 * copies.min(20);
        gpu.gemm_bf16(&x, &ws[0], &mut y, &mut scratch, t, n, k)?;
        gpu.sync()?;
        let start = std::time::Instant::now();
        for r in 0..reps {
            gpu.gemm_bf16(&x, &ws[r % copies], &mut y, &mut scratch, t, n, k)?;
        }
        gpu.sync()?;
        let s = start.elapsed().as_secs_f64() / reps as f64;
        let bytes = (n * k * 2) as f64;
        total_bytes += bytes;
        total_s += s;
        println!(
            "{n:6} x {k:5}: {:7.1} us, {:6.0} GB/s",
            s * 1e6,
            bytes / s / 1e9
        );
    }
    println!(
        "all shapes: {:.0} GB/s weighted by bytes",
        total_bytes / total_s / 1e9
    );
    Ok(())
}
