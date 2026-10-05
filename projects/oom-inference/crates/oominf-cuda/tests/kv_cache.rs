//! KV cache formats: fp32 rows round-trip exactly; Turbo rows round-trip with the
//! distortion of a Lloyd-Max quantizer for a unit Gaussian (the rotation makes the
//! coordinates near-Gaussian, the block scale makes them unit variance). Needs a GPU:
//! `cargo test --release -p oominf-cuda --test kv_cache -- --ignored --nocapture`.

use anyhow::Result;
use oominf_core::{Attention, KvFormat, Memory};
use oominf_cuda::Gpu;

/// Deterministic standard normal samples (xorshift64* and Box-Muller).
fn gaussian(n: usize, mut state: u64) -> Vec<f32> {
    let mut next = || {
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        ((state.wrapping_mul(0x2545_f491_4f6c_dd1d) >> 11) as f64 + 0.5) / (1u64 << 53) as f64
    };
    (0..n)
        .map(|_| {
            let (u, v) = (next(), next());
            ((-2.0 * u.ln()).sqrt() * (std::f64::consts::TAU * v).cos()) as f32
        })
        .collect()
}

/// Relative squared error of reading back `x` (`[t, kv_heads, d]`) stored as `format`.
fn round_trip(
    gpu: &Gpu,
    x: &[f32],
    format: KvFormat,
    t: usize,
    kvh: usize,
    d: usize,
) -> Result<f64> {
    let src = gpu.upload_f32(x)?;
    let mut cache = gpu.zeros_bytes(t * kvh * format.row_bytes(true, d))?;
    gpu.kv_append(&src, &mut cache, format, true, 0, t, kvh, d)?;
    let mut back = gpu.zeros(x.len())?;
    gpu.kv_read(&cache, &mut back, format, true, t, kvh, d)?;
    let y = gpu.download_f32(&back)?;
    let (err, norm) = x.iter().zip(&y).fold((0f64, 0f64), |(e, n), (&a, &b)| {
        (e + (a as f64 - b as f64).powi(2), n + (a as f64).powi(2))
    });
    Ok(err / norm)
}

#[test]
#[ignore = "needs a GPU"]
fn formats_round_trip() -> Result<()> {
    let gpu = Gpu::new(0)?;
    let (t, kvh, d) = (64, 2, 256);
    // Heavy-tailed rows: a few large coordinates, as real keys have.
    let mut x = gaussian(t * kvh * d, 11);
    for (i, v) in x.iter_mut().enumerate() {
        if i % 37 == 0 {
            *v *= 8.0;
        }
    }
    assert_eq!(round_trip(&gpu, &x, KvFormat::F32, t, kvh, d)?, 0.0);
    // Lloyd-Max distortion for a unit Gaussian: 0.1175 (2 bits), 0.0345 (3), 0.0095
    // (4), 0.0025 (5), 0.0007 (6), about 0.00004 (8).
    for (bits, max) in [
        (2u8, 0.14),
        (3, 0.045),
        (4, 0.013),
        (5, 0.0035),
        (6, 0.001),
        (8, 0.0001),
    ] {
        let f = KvFormat::Turbo {
            k_bits: bits,
            v_bits: bits,
        };
        let e = round_trip(&gpu, &x, f, t, kvh, d)?;
        println!("{bits}-bit relative squared error {e:.4}");
        assert!(e < max, "{bits}-bit error {e:.4} above {max}");
    }
    Ok(())
}

/// Attention over a cache stored as `format` against the same attention over an
/// fp32 cache: the relative error of the outputs for `t` queries over `kv_len` keys.
fn attention_error(gpu: &Gpu, format: KvFormat, t: usize, kv_len: usize) -> Result<f64> {
    let (heads, kvh, d) = (24, 2, 256);
    let q = gaussian(t * heads * d, 3);
    let k = gaussian(kv_len * kvh * d, 5);
    let v = gaussian(kv_len * kvh * d, 7);
    let mask = vec![1u8; t * kv_len];
    let mask = gpu.upload_bytes(&mask)?;
    let q = gpu.upload_f32(&q)?;
    let run = |f: KvFormat| -> Result<Vec<f32>> {
        let mut kc = gpu.zeros_bytes(kv_len * kvh * f.row_bytes(true, d))?;
        let mut vc = gpu.zeros_bytes(kv_len * kvh * f.row_bytes(false, d))?;
        gpu.kv_append(&gpu.upload_f32(&k)?, &mut kc, f, true, 0, kv_len, kvh, d)?;
        gpu.kv_append(&gpu.upload_f32(&v)?, &mut vc, f, false, 0, kv_len, kvh, d)?;
        let mut out = gpu.zeros(t * heads * d)?;
        let mut ws = oominf_core::Workspace::new();
        gpu.attention(
            &mut ws,
            &q,
            &kc,
            &vc,
            f,
            &mask,
            &mut out,
            t,
            heads,
            kvh,
            d,
            kv_len,
            kv_len,
            1.0 / 16.0,
        )?;
        gpu.download_f32(&out)
    };
    let (exact, approx) = (run(KvFormat::F32)?, run(format)?);
    let (err, norm) = exact
        .iter()
        .zip(&approx)
        .fold((0f64, 0f64), |(e, n), (&a, &b)| {
            (e + (a as f64 - b as f64).powi(2), n + (a as f64).powi(2))
        });
    Ok((err / norm).sqrt())
}

#[test]
#[ignore = "needs a GPU"]
fn attention_on_compressed_cache() -> Result<()> {
    let gpu = Gpu::new(0)?;
    for t in [1, 3, 64] {
        let e = attention_error(
            &gpu,
            KvFormat::Turbo {
                k_bits: 8,
                v_bits: 8,
            },
            t,
            4096,
        )?;
        println!("t={t}: k8v8 relative output error {e:.3e}");
        assert!(e < 1e-2, "k8v8 attention error {e:.3e} for t={t}");
    }
    Ok(())
}

#[test]
#[ignore = "needs a GPU"]
fn decode_attention_timing() -> Result<()> {
    let gpu = Gpu::new(0)?;
    let (heads, kvh, d, kv_len) = (24, 2, 256, 95_000);
    let q = gpu.upload_f32(&gaussian(heads * d, 3))?;
    let k = gaussian(kv_len * kvh * d, 5);
    let v = gaussian(kv_len * kvh * d, 7);
    for (name, keep) in [("dense", 1usize), ("2%", 50)] {
        let mask: Vec<u8> = (0..kv_len).map(|j| u8::from(j % keep == 0)).collect();
        let mask = gpu.upload_bytes(&mask)?;
        for f in [
            KvFormat::F32,
            KvFormat::Turbo {
                k_bits: 8,
                v_bits: 6,
            },
        ] {
            let mut kc = gpu.zeros_bytes(kv_len * kvh * f.row_bytes(true, d))?;
            let mut vc = gpu.zeros_bytes(kv_len * kvh * f.row_bytes(false, d))?;
            gpu.kv_append(&gpu.upload_f32(&k)?, &mut kc, f, true, 0, kv_len, kvh, d)?;
            gpu.kv_append(&gpu.upload_f32(&v)?, &mut vc, f, false, 0, kv_len, kvh, d)?;
            let mut out = gpu.zeros(heads * d)?;
            let mut ws = oominf_core::Workspace::new();
            let mut run = || {
                gpu.attention(
                    &mut ws, &q, &kc, &vc, f, &mask, &mut out, 1, heads, kvh, d, kv_len, kv_len,
                    0.0625,
                )
            };
            run()?;
            gpu.sync()?;
            let t = std::time::Instant::now();
            for _ in 0..50 {
                run()?;
            }
            gpu.sync()?;
            println!(
                "{name} mask, {f:?}: {:.1} us per decode attention",
                t.elapsed().as_secs_f64() / 50.0 * 1e6
            );
        }
    }
    Ok(())
}

/// Prefill-sized attention under sparse masks (each query keeps a random tenth of
/// the positions up to its own, plus itself) matches an f64 reference, on an fp32
/// cache and with `max_visible` at the largest row count.
#[test]
#[ignore = "needs a GPU"]
fn sparse_attention_matches_reference() -> Result<()> {
    let gpu = Gpu::new(0)?;
    let (heads, kvh, d, kv_len, t) = (24, 2, 256, 3000, 37);
    let g = heads / kvh;
    let start = kv_len - t;
    let q = gaussian(t * heads * d, 11);
    let k = gaussian(kv_len * kvh * d, 13);
    let v = gaussian(kv_len * kvh * d, 17);
    let mut state = 23u64;
    let mut mask = vec![0u8; t * kv_len];
    for i in 0..t {
        for j in 0..=start + i {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            mask[i * kv_len + j] = u8::from(j == start + i || (state >> 33).is_multiple_of(10));
        }
    }
    let max_visible = (0..t)
        .map(|i| {
            mask[i * kv_len..(i + 1) * kv_len]
                .iter()
                .filter(|&&m| m != 0)
                .count()
        })
        .max()
        .unwrap();
    let f = KvFormat::F32;
    let mut kc = gpu.zeros_bytes(kv_len * kvh * f.row_bytes(true, d))?;
    let mut vc = gpu.zeros_bytes(kv_len * kvh * f.row_bytes(false, d))?;
    gpu.kv_append(&gpu.upload_f32(&k)?, &mut kc, f, true, 0, kv_len, kvh, d)?;
    gpu.kv_append(&gpu.upload_f32(&v)?, &mut vc, f, false, 0, kv_len, kvh, d)?;
    let mut out = gpu.zeros(t * heads * d)?;
    let mut ws = oominf_core::Workspace::new();
    let scale = 1.0 / 16.0;
    gpu.attention(
        &mut ws,
        &gpu.upload_f32(&q)?,
        &kc,
        &vc,
        f,
        &gpu.upload_bytes(&mask)?,
        &mut out,
        t,
        heads,
        kvh,
        d,
        kv_len,
        max_visible,
        scale,
    )?;
    let got = gpu.download_f32(&out)?;
    let mut worst = 0f64;
    for i in 0..t {
        for h in 0..heads {
            let qr = &q[(i * heads + h) * d..][..d];
            let kvhh = h / g;
            let scores: Vec<(usize, f64)> = (0..kv_len)
                .filter(|&j| mask[i * kv_len + j] != 0)
                .map(|j| {
                    let kr = &k[(j * kvh + kvhh) * d..][..d];
                    let s: f64 = qr.iter().zip(kr).map(|(&a, &b)| a as f64 * b as f64).sum();
                    (j, s * scale as f64)
                })
                .collect();
            let m = scores
                .iter()
                .map(|&(_, s)| s)
                .fold(f64::NEG_INFINITY, f64::max);
            let den: f64 = scores.iter().map(|&(_, s)| (s - m).exp()).sum();
            for c in 0..d {
                let num: f64 = scores
                    .iter()
                    .map(|&(j, s)| (s - m).exp() * v[(j * kvh + kvhh) * d + c] as f64)
                    .sum();
                let want = num / den;
                let err = (got[(i * heads + h) * d + c] as f64 - want).abs();
                worst = worst.max(err);
            }
        }
    }
    println!("sparse attention: max abs error {worst:.2e} (max_visible {max_visible})");
    assert!(worst < 1e-4, "max abs error {worst:.2e}");
    Ok(())
}

/// A cache in host memory (`zeros_bytes_host`) holds the same rows and gives
/// bit-identical attention to one in device memory; host buffers allocate and free
/// cleanly in a loop.
#[test]
#[ignore = "needs a GPU"]
fn host_cache_matches_device() -> Result<()> {
    let gpu = Gpu::new(0)?;
    let (heads, kvh, d, kv_len, t) = (24, 2, 256, 3000, 2);
    let f = KvFormat::Turbo {
        k_bits: 8,
        v_bits: 6,
    };
    let k = gpu.upload_f32(&gaussian(kv_len * kvh * d, 21))?;
    let v = gpu.upload_f32(&gaussian(kv_len * kvh * d, 22))?;
    let q = gpu.upload_f32(&gaussian(t * heads * d, 23))?;
    let mask: Vec<u8> = (0..t * kv_len).map(|i| u8::from(i % 7 != 0)).collect();
    let mask = gpu.upload_bytes(&mask)?;
    let mut outs = Vec::new();
    for host in [false, true] {
        let alloc = |n| {
            if host {
                gpu.zeros_bytes_host(n)
            } else {
                gpu.zeros_bytes(n)
            }
        };
        let mut kc = alloc(kv_len * kvh * f.row_bytes(true, d))?;
        let mut vc = alloc(kv_len * kvh * f.row_bytes(false, d))?;
        gpu.kv_append(&k, &mut kc, f, true, 0, kv_len, kvh, d)?;
        gpu.kv_append(&v, &mut vc, f, false, 0, kv_len, kvh, d)?;
        let mut out = gpu.zeros(t * heads * d)?;
        let mut ws = oominf_core::Workspace::new();
        gpu.attention(
            &mut ws, &q, &kc, &vc, f, &mask, &mut out, t, heads, kvh, d, kv_len, kv_len, 0.0625,
        )?;
        outs.push((gpu.download_bytes(&kc)?, gpu.download_f32(&out)?));
    }
    assert_eq!(outs[0].0, outs[1].0, "cached rows differ");
    let same = outs[0]
        .1
        .iter()
        .zip(&outs[1].1)
        .all(|(a, b)| a.to_bits() == b.to_bits());
    assert!(same, "attention over a host cache differs");
    for i in 0..50 {
        let mut b = gpu.zeros_bytes_host(1 << 20)?;
        gpu.copy_bytes(&gpu.upload_bytes(&[i as u8; 64])?, 0, &mut b, 0, 64)?;
        assert_eq!(gpu.download_bytes(&b)?[..64], [i as u8; 64]);
    }
    gpu.sync()?;
    Ok(())
}
