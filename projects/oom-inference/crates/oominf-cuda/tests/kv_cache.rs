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
