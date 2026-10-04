//! `moe_tiled` (tensor-core NVFP4 grouped GEMM) against an f64 reference on random
//! records, plus its throughput at the prefill shape. Needs a GPU:
//! `cargo test --release -p oominf-cuda --test moe_tiled -- --ignored --nocapture`.

use std::time::Instant;

use anyhow::Result;
use oominf_core::{Experts, Memory, View};
use oominf_cuda::Gpu;

/// Deterministic xorshift64*.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    /// Uniform in [-1, 1) with a full fp32 mantissa.
    fn unit(&mut self) -> f32 {
        (self.next() >> 40) as f32 / (1u64 << 23) as f32 - 1.0
    }
}

fn e2m1(code: u8) -> f64 {
    const MAG: [f64; 8] = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0];
    let v = MAG[(code & 7) as usize];
    if code & 8 != 0 { -v } else { v }
}

fn fp8_e4m3(b: u8) -> f64 {
    let (e, m) = (((b >> 3) & 15) as i32, (b & 7) as f64);
    let v = if e == 0 {
        m / 8.0 * 2f64.powi(-6)
    } else {
        (1.0 + m / 8.0) * 2f64.powi(e - 7)
    };
    if b & 0x80 != 0 { -v } else { v }
}

/// One projection's records: `[scale2 as f32, pad..][weights N x K/2][scales N x K/16]`.
struct Records {
    n: usize,
    k: usize,
    w_off: usize,
    s_off: usize,
    stride: usize,
    bytes: Vec<u8>,
}

impl Records {
    fn random(rng: &mut Rng, experts: usize, n: usize, k: usize) -> Self {
        let w_off = 64;
        let s_off = w_off + n * k / 2;
        let stride = (s_off + n * k / 16).next_multiple_of(4096);
        let mut bytes = vec![0u8; experts * stride];
        for e in 0..experts {
            let r = &mut bytes[e * stride..(e + 1) * stride];
            let s2 = 0.01 + 0.02 * rng.unit().abs();
            r[..4].copy_from_slice(&s2.to_le_bytes());
            for b in &mut r[w_off..s_off] {
                *b = rng.next() as u8;
            }
            // Positive, finite scales between about 2^-2 and 2^2.
            for b in &mut r[s_off..s_off + n * k / 16] {
                *b = 0x28 + (rng.next() % 0x28) as u8;
            }
        }
        Records {
            n,
            k,
            w_off,
            s_off,
            stride,
            bytes,
        }
    }

    /// `w[r][k]` of expert `e`, decoded exactly.
    fn weight(&self, e: usize, r: usize, k: usize) -> f64 {
        let rec = &self.bytes[e * self.stride..];
        let s2 = f32::from_le_bytes(rec[..4].try_into().unwrap()) as f64;
        let byte = rec[self.w_off + r * self.k / 2 + k / 2];
        let code = if k.is_multiple_of(2) {
            byte & 15
        } else {
            byte >> 4
        };
        e2m1(code) * fp8_e4m3(rec[self.s_off + r * self.k / 16 + k / 16]) * s2
    }
}

/// Output of [`run`]: `y` (`[A, N]`), each assignment's token row, the expert
/// offsets and seconds per launch.
struct Run {
    y: Vec<f32>,
    rows: Vec<i32>,
    off: Vec<i32>,
    secs: f64,
}

/// Runs `moe_tiled` for experts with `counts[e]` assignments each, reading token rows
/// `rows[a]` of `x` (`[tokens, K]`), `launches` more times for timing.
fn run(
    gpu: &Gpu,
    recs: &Records,
    x: &[f32],
    counts: &[usize],
    rng: &mut Rng,
    launches: usize,
) -> Result<Run> {
    let tokens = x.len() / recs.k;
    let mut off = vec![0i32];
    for &c in counts {
        off.push(off.last().unwrap() + c as i32);
    }
    let a = *off.last().unwrap() as usize;
    let rows: Vec<i32> = (0..a)
        .map(|_| (rng.next() % tokens as u64) as i32)
        .collect();
    let dev = gpu.upload_bytes(&recs.bytes)?;
    let base = gpu.bytes_addr(&dev);
    let addrs: Vec<u64> = (0..counts.len())
        .map(|e| base + (e * recs.stride) as u64)
        .collect();
    let mut recs_dev = gpu.zeros_u64(addrs.len())?;
    gpu.write_u64(&addrs, &mut recs_dev)?;
    let off_dev = gpu.upload_i32(&off)?;
    let rows_dev = gpu.upload_i32(&rows)?;
    let x_dev = gpu.upload_f32(x)?;
    let mut y = gpu.zeros(a * recs.n)?;
    let max_n = counts.iter().copied().max().unwrap_or(0);
    let call = |y: &mut _| {
        gpu.moe_tiled(
            &View::new(&recs_dev, 0, counts.len()),
            &View::new(&off_dev, 0, counts.len() + 1),
            Some(&View::new(&rows_dev, 0, a)),
            counts.len(),
            max_n,
            &x_dev,
            y,
            recs.n,
            recs.k,
            (recs.w_off, recs.s_off, 0),
        )
    };
    call(&mut y)?;
    gpu.sync()?;
    let t = Instant::now();
    for _ in 0..launches {
        call(&mut y)?;
    }
    gpu.sync()?;
    let secs = t.elapsed().as_secs_f64() / launches.max(1) as f64;
    Ok(Run {
        y: gpu.download_f32(&y)?,
        rows,
        off,
        secs,
    })
}

fn check(n: usize, k: usize, counts: &[usize]) -> Result<()> {
    let gpu = Gpu::new(0)?;
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15 ^ (n * k) as u64);
    let recs = Records::random(&mut rng, counts.len(), n, k);
    let tokens = 64;
    let x: Vec<f32> = (0..tokens * k).map(|_| 2.0 * rng.unit()).collect();
    let Run { y, rows, off, .. } = run(&gpu, &recs, &x, counts, &mut rng, 0)?;
    let mut worst = 0f64;
    for e in 0..counts.len() {
        for a in off[e] as usize..off[e + 1] as usize {
            let xr = &x[rows[a] as usize * k..][..k];
            for r in 0..n {
                let (mut sum, mut mag) = (0f64, 0f64);
                for (kk, &xv) in xr.iter().enumerate() {
                    let p = xv as f64 * recs.weight(e, r, kk);
                    sum += p;
                    mag += p.abs();
                }
                let err = (y[a * n + r] as f64 - sum).abs() / mag.max(f64::MIN_POSITIVE);
                worst = worst.max(err);
            }
        }
    }
    println!("N={n} K={k} counts={counts:?}: worst error / sum|x*w| = {worst:.3e}");
    // fp32 accumulation over K terms: well within K * 2^-24 of the magnitude.
    assert!(
        worst < 2e-6,
        "moe_tiled error {worst:.3e} exceeds fp32 accumulation"
    );
    Ok(())
}

#[test]
#[ignore = "needs a GPU"]
fn matches_f64_reference() -> Result<()> {
    // Partial tiles in both dimensions, an empty expert, and both expert shapes.
    check(200, 96, &[1, 0, 37, 70])?;
    check(640, 2560, &[3, 33, 64])?;
    check(2560, 640, &[17, 40])?;
    Ok(())
}

#[test]
#[ignore = "needs a GPU"]
fn prefill_throughput() -> Result<()> {
    // One layer's gate projection for 4096 tokens: 512 experts, top-10, 80 each.
    let gpu = Gpu::new(0)?;
    let mut rng = Rng(7);
    let (n, k, experts, per) = (640, 2560, 512, 80);
    let recs = Records::random(&mut rng, experts, n, k);
    let x: Vec<f32> = (0..4096 * k).map(|_| rng.unit()).collect();
    let secs = run(&gpu, &recs, &x, &vec![per; experts], &mut rng, 20)?.secs;
    let flops = 2.0 * (experts * per * n * k) as f64;
    println!(
        "moe_tiled {n}x{k}, {experts} experts x {per}: {:.3} ms, {:.1} TFLOP/s",
        secs * 1e3,
        flops / secs / 1e12
    );
    Ok(())
}
