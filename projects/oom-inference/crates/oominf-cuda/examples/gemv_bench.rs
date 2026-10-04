//! Times the decode GEMV (`gemm_bf16` with t = 1) on the model's dense shapes and
//! reports achieved memory bandwidth. `cargo run --release --example gemv_bench`.

use std::time::Instant;

use oominf_core::{Linear, Memory};
use oominf_cuda::Gpu;

fn main() -> anyhow::Result<()> {
    let gpu = Gpu::new(0)?;
    // (name, N, K): GDN in_proj, attention in_proj, o/out proj, hc up/down, router,
    // shared expert, lm_head.
    let shapes = [
        ("gdn.in_proj", 16480, 2560),
        ("attn.in_proj", 13952, 2560),
        ("out_proj", 2560, 6144),
        ("hc.up", 10240, 320),
        ("hc.down", 320, 10240),
        ("hc.inject", 4, 10240),
        ("router", 512, 2560),
        ("shared.gate", 640, 2560),
        ("shared.down", 2560, 640),
        ("lm_head", 248320, 2560),
    ];
    let mut scratch = gpu.upload_bf16(&[0u16; 16])?;
    for (name, n, k) in shapes {
        let w = gpu.upload_bf16(
            &(0..n * k)
                .map(|i| 0x3c00 + (i % 97) as u16)
                .collect::<Vec<_>>(),
        )?;
        let x = gpu.upload_f32(&(0..k).map(|i| (i % 13) as f32 * 0.01).collect::<Vec<_>>())?;
        let mut y = gpu.zeros(n)?;
        for _ in 0..3 {
            gpu.gemm_bf16(&x, &w, &mut y, &mut scratch, 1, n, k)?;
        }
        gpu.sync()?;
        let iters = (2_000_000_000 / (n * k * 2)).clamp(10, 2000);
        let t = Instant::now();
        for _ in 0..iters {
            gpu.gemm_bf16(&x, &w, &mut y, &mut scratch, 1, n, k)?;
        }
        gpu.sync()?;
        let us = t.elapsed().as_secs_f64() * 1e6 / iters as f64;
        let gbs = (n * k * 2) as f64 / us / 1e3;
        println!("{name:<14} N={n:<7} K={k:<6} {us:8.1} us  {gbs:7.1} GB/s");
    }
    Ok(())
}
