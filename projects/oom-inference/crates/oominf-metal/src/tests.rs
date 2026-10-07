use super::Gpu;

#[test]
fn causal_convolution_keeps_history_and_zero_pads_the_first_token() {
    let gpu = Gpu::new().unwrap();
    let w = gpu
        .upload_bf16(&[1f32, 2., 3., -1., 0.5, 2.].map(|v| (v.to_bits() >> 16) as u16))
        .unwrap();
    let mut history = gpu.zeros(4).unwrap();
    for (input, expected) in [
        ([1., 2.], [3f32, 4.]),
        ([2., -1.], [8., -1.]),
        ([0., 3.], [5., 3.5]),
    ] {
        let x = gpu.upload_f32(&input).unwrap();
        let mut out = gpu.uninit(2).unwrap();
        gpu.causal_conv(&x, &w, &mut history, &mut out, 2, 3)
            .unwrap();
        let got = gpu.download_f32(&out).unwrap();
        for i in 0..2 {
            let want = expected[i] / (1. + (-expected[i]).exp());
            assert!((got[i] - want).abs() < 1e-5, "{} != {want}", got[i]);
        }
    }
    assert_eq!(gpu.download_f32(&history).unwrap(), [2., -1., 0., 3.]);
}

#[test]
fn delta_recurrence_matches_f64_reference_across_steps_and_grouped_heads() {
    let gpu = Gpu::new().unwrap();
    let (nk, nv, dk, dv) = (2, 4, 4, 3);
    let mut state = gpu.zeros(nv * dv * dk).unwrap();
    let mut reference = vec![0f64; nv * dv * dk];
    let log_a = gpu
        .upload_bf16(&[0f32, 0.5, -0.5, 1.].map(|v| (v.to_bits() >> 16) as u16))
        .unwrap();
    let bias = gpu
        .upload_bf16(&[0f32, -1., 1., 0.25].map(|v| (v.to_bits() >> 16) as u16))
        .unwrap();
    for step in 0..3 {
        let mut values: Vec<f32> = (0..2 * nk * dk + nv * dv)
            .map(|i| ((i + step * 3) % 11) as f32 / 7. - 0.6)
            .collect();
        for head in 0..2 * nk {
            let base = head * dk;
            let sum: f32 = values[base..base + dk].iter().map(|v| v * v).sum();
            for v in &mut values[base..base + dk] {
                *v /= (sum + 1e-6).sqrt();
            }
        }
        let av = [-3f32, 1., -0.5, 2.];
        let bv = [0.25f32, -1., 2., 0.];
        let qkv = gpu.upload_f32(&values).unwrap();
        let a = gpu.upload_f32(&av).unwrap();
        let b = gpu.upload_f32(&bv).unwrap();
        let mut out = gpu.uninit(nv * dv).unwrap();
        gpu.delta_step(
            &qkv, &a, &b, &log_a, &bias, &mut state, &mut out, nk, nv, dk, dv,
        )
        .unwrap();
        let got = gpu.download_f32(&out).unwrap();
        for hv in 0..nv {
            let hk = hv / (nv / nk);
            let dt = av[hv] as f64 + [0., -1., 1., 0.25][hv];
            let decay = (-[0f64, 0.5, -0.5, 1.][hv].exp() * (1. + dt.exp()).ln()).exp();
            let beta = 1. / (1. + (-(bv[hv] as f64)).exp());
            for c in 0..dv {
                let at = (hv * dv + c) * dk;
                for v in &mut reference[at..at + dk] {
                    *v *= decay;
                }
                let prediction: f64 = (0..dk)
                    .map(|j| reference[at + j] * values[nk * dk + hk * dk + j] as f64)
                    .sum();
                let delta = (values[2 * nk * dk + hv * dv + c] as f64 - prediction) * beta;
                for j in 0..dk {
                    reference[at + j] += values[nk * dk + hk * dk + j] as f64 * delta;
                }
                let want: f64 = (0..dk)
                    .map(|j| reference[at + j] * values[hk * dk + j] as f64)
                    .sum::<f64>()
                    / (dk as f64).sqrt();
                assert!(
                    (got[hv * dv + c] as f64 - want).abs() < 2e-6,
                    "{} != {want}",
                    got[hv * dv + c]
                );
            }
        }
        let actual = gpu.download_f32(&state).unwrap();
        assert!(
            actual
                .iter()
                .zip(&reference)
                .all(|(&a, &b)| (a as f64 - b).abs() < 2e-6)
        );
    }
}

#[test]
fn grouped_attention_matches_f64_softmax_and_partial_rope_preserves_tail() {
    let gpu = Gpu::new().unwrap();
    let qv: Vec<f32> = (0..16).map(|i| (i as f32 - 7.) / 8.).collect();
    let kv: Vec<f32> = (0..24).map(|i| ((i * 7 % 17) as f32 - 8.) / 8.).collect();
    let vv: Vec<f32> = (0..24).map(|i| ((i * 3 % 13) as f32 - 6.) / 6.).collect();
    let q = gpu.upload_f32(&qv).unwrap();
    let k = gpu.upload_f32(&kv).unwrap();
    let v = gpu.upload_f32(&vv).unwrap();
    let mut out = gpu.uninit(16).unwrap();
    let mut scores = gpu.uninit(12).unwrap();
    gpu.gqa_step(&q, &k, &v, &mut out, &mut scores, 4, 2, 4, 3)
        .unwrap();
    let got = gpu.download_f32(&out).unwrap();
    for h in 0..4 {
        let raw: Vec<f64> = (0..3)
            .map(|t| {
                (0..4)
                    .map(|c| qv[h * 4 + c] as f64 * kv[(t * 2 + h / 2) * 4 + c] as f64)
                    .sum::<f64>()
                    / 2.
            })
            .collect();
        let max = raw.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let exp: Vec<_> = raw.iter().map(|v| (v - max).exp()).collect();
        let sum: f64 = exp.iter().sum();
        for c in 0..4 {
            let want: f64 = (0..3)
                .map(|t| exp[t] * vv[(t * 2 + h / 2) * 4 + c] as f64)
                .sum::<f64>()
                / sum;
            assert!((got[h * 4 + c] as f64 - want).abs() < 1e-6);
        }
    }
    let mut x = gpu.upload_f32(&[1., 2., 3., 4., 5., 6., 7., 8.]).unwrap();
    gpu.rope_half(&mut x, 1, 8, 4, 7, 100.).unwrap();
    let actual = gpu.download_f32(&x).unwrap();
    for (i, angle) in [7f32, 0.7].into_iter().enumerate() {
        let a = [1., 2.][i];
        let b = [3., 4.][i];
        assert!((actual[i] - (a * angle.cos() - b * angle.sin())).abs() < 1e-6);
        assert!((actual[i + 2] - (b * angle.cos() + a * angle.sin())).abs() < 1e-6);
    }
    assert_eq!(&actual[4..], &[5., 6., 7., 8.]);
}
use oominf_core::{Linear, Memory};

#[test]
fn shared_buffers_preserve_typed_contents_and_bounds() {
    let gpu = Gpu::new().unwrap();
    let mut a = gpu.upload_f32(&[1., 2., 3.]).unwrap();
    gpu.write_f32_at(&[9.], &mut a, 1).unwrap();
    assert_eq!(gpu.download_f32(&a).unwrap(), [1., 9., 3.]);
    assert!(gpu.write_f32_at(&[1., 2.], &mut a, 2).is_err());
    let (free, total) = gpu.mem_info().unwrap();
    assert!(free <= total && total > 0);
}

#[test]
fn shared_memory_limit_rejects_large_allocations_before_metal_allocates() {
    let gpu = Gpu::with_memory_limit(1024 * 1024).unwrap();
    assert!(gpu.zeros_bytes(2 * 1024 * 1024).is_err());
    let buffer = gpu.zeros_bytes(4096).unwrap();
    let (free, total) = gpu.mem_info().unwrap();
    assert!(free < total && total <= 1024 * 1024);
    assert_eq!(gpu.download_bytes(&buffer).unwrap(), vec![0; 4096]);
}

#[test]
fn bf16_gemm_runs_on_metal_for_multiple_rows_and_tokens() {
    let gpu = Gpu::new().unwrap();
    let x = gpu.upload_f32(&[1., 2., 3., 4., 5., 6.]).unwrap();
    let w = gpu
        .upload_bf16(&[1f32, 0., -1., 2., 1., 0.].map(|v| (v.to_bits() >> 16) as u16))
        .unwrap();
    let mut y = gpu.zeros(4).unwrap();
    let mut scratch = gpu.uninit_bf16(6).unwrap();
    gpu.gemm_bf16(&x, &w, &mut y, &mut scratch, 2, 2, 3)
        .unwrap();
    assert_eq!(gpu.download_f32(&y).unwrap(), [-2., 4., -2., 13.]);
}

#[test]
fn fp8_gemm_decodes_finite_codes_and_partial_scale_blocks() {
    let gpu = Gpu::new().unwrap();
    let k = 130;
    let values: Vec<f32> = (0..k).map(|i| (i as f32 - 65.) / 128.).collect();
    let packed: Vec<u8> = (0..2 * k)
        .map(|i| {
            let code = (i % 256) as u8;
            if code & 0x7f == 0x7f { code - 1 } else { code }
        })
        .collect();
    let scales = [0.25, 2., 1., 0.5];
    let x = gpu.upload_f32(&values).unwrap();
    let q = gpu.upload_bytes(&packed).unwrap();
    let s = gpu.upload_f32(&scales).unwrap();
    let mut y = gpu.zeros(2).unwrap();
    let mut scratch = gpu.uninit_bf16(k).unwrap();
    gpu.gemm_fp8(&x, &q, &s, &mut y, &mut scratch, 1, 2, k)
        .unwrap();
    let got = gpu.download_f32(&y).unwrap();
    for row in 0..2 {
        let want: f32 = (0..k)
            .map(|c| {
                values[c]
                    * oominf_core::fp8::e4m3_to_f32(packed[row * k + c])
                    * scales[row * 2 + c / 128]
            })
            .sum();
        assert!((got[row] - want).abs() < 1e-4, "{} != {want}", got[row]);
    }
}

#[test]
fn l2norm_changes_only_selected_heads_and_column_copies_preserve_rows() {
    use oominf_core::{Elementwise, Norm};
    let gpu = Gpu::new().unwrap();
    let mut x = gpu.upload_f32(&[9., 3., 4., 8., 7., 0., 0., 6.]).unwrap();
    gpu.l2norm_heads(&mut x, 2, 4, 1, 1, 2, 1e-6).unwrap();
    let mut columns = gpu.zeros(4).unwrap();
    gpu.copy_cols(&x, &mut columns, 2, 4, 1, 2).unwrap();
    let got = gpu.download_f32(&columns).unwrap();
    assert!((got[0] - 0.6).abs() < 1e-6);
    assert!((got[1] - 0.8).abs() < 1e-6);
    assert_eq!(&got[2..], &[0., 0.]);
    let mut destination = gpu.upload_f32(&[1.; 8]).unwrap();
    gpu.put_cols(&columns, &mut destination, 2, 4, 1, 2)
        .unwrap();
    let copied = gpu.download_f32(&destination).unwrap();
    for i in [0, 3, 4, 7] {
        assert_eq!(copied[i], 1.);
    }
    assert_eq!(&copied[1..3], &got[..2]);
    let input = gpu.download_f32(&x).unwrap();
    assert_eq!([input[0], input[3], input[4], input[7]], [9., 8., 7., 6.]);
}

#[test]
fn nvfp4_gemm_matches_cpu_reference_with_signs_and_scales() {
    let gpu = Gpu::new().unwrap();
    let values: Vec<f32> = (0..64).map(|i| (i as f32 - 31.) / 17.).collect();
    let packed: Vec<u8> = (0..48).map(|i| ((i * 13) % 256) as u8).collect();
    let scales = [0x38, 0x30, 0x40, 0x38, 0x28, 0x40];
    let x = gpu.upload_f32(&values).unwrap();
    let q = gpu.upload_bytes(&packed).unwrap();
    let s = gpu.upload_bytes(&scales).unwrap();
    let mut y = gpu.zeros(6).unwrap();
    gpu.gemm_nvfp4(&x, &q, &s, 0.75, &mut y, 2, 3, 32).unwrap();
    let code = [
        0., 0.5, 1., 1.5, 2., 3., 4., 6., -0., -0.5, -1., -1.5, -2., -3., -4., -6.,
    ];
    let scale = [1., 0.5, 2., 1., 0.25, 2.];
    let got = gpu.download_f32(&y).unwrap();
    for token in 0..2 {
        for row in 0..3 {
            let want: f32 = (0..32)
                .map(|c| {
                    let byte = packed[row * 16 + c / 2];
                    let nibble = (byte >> ((c % 2) * 4)) & 15;
                    values[token * 32 + c] * code[nibble as usize] * scale[row * 2 + c / 16] * 0.75
                })
                .sum();
            assert!(
                (got[token * 3 + row] - want).abs() < 1e-4,
                "{} != {want}",
                got[token * 3 + row]
            );
        }
    }
}

#[test]
fn command_ordering_and_norm_match_reference() {
    use oominf_core::{Elementwise, Norm};
    let gpu = Gpu::new().unwrap();
    let x = gpu.upload_f32(&[1., 2., 3., 4.]).unwrap();
    let zero = gpu.zeros(4).unwrap();
    let w = gpu
        .upload_bf16(&[1f32, 2.].map(|v| (v.to_bits() >> 16) as u16))
        .unwrap();
    let mut norm = gpu.uninit(4).unwrap();
    let mut out = gpu.uninit(4).unwrap();
    gpu.rmsnorm_groups(&x, &w, &mut norm, 1, 4, 2, 1e-6, 0.)
        .unwrap();
    gpu.silu_mul(&norm, &x, &mut out, 4).unwrap();
    let mut summed = gpu.uninit(4).unwrap();
    gpu.add(&out, &zero, &mut summed, 4).unwrap();
    let got = gpu.download_f32(&summed).unwrap();
    for i in 0..4 {
        let values = [1f32, 2., 3., 4.];
        let group = i / 2;
        let inv = ((values[group * 2].powi(2) + values[group * 2 + 1].powi(2)) / 2. + 1e-6)
            .sqrt()
            .recip();
        let value = values[i] * inv * (if i % 2 == 0 { 1. } else { 2. });
        let want = value / (1. + (-value).exp()) * values[i];
        assert!((got[i] - want).abs() < 1e-5);
    }
}

#[test]
#[allow(clippy::let_unit_value)] // Queue/event handles may become asynchronous later.
fn uncached_disk_reads_copy_into_metal_buffers_by_gpu_address() {
    use oominf_core::Transfer;
    use oominf_tiers::host::{DirectReader, PinnedArena, ReadJob};
    use std::sync::Arc;
    let gpu = Arc::new(Gpu::new().unwrap());
    let file = tempfile::NamedTempFile::new().unwrap();
    let bytes: Vec<u8> = (0..8192).map(|i| (i * 17 % 251) as u8).collect();
    std::fs::write(file.path(), &bytes).unwrap();
    let arena = PinnedArena::new(gpu.clone(), 2, 4096).unwrap();
    let destination = gpu.zeros_bytes(8192).unwrap();
    let address = gpu.bytes_addr(&destination);
    let queue = gpu.copy_queue().unwrap();
    let mut reader = DirectReader::open(file.path(), 2).unwrap();
    reader
        .submit(
            (0..2)
                .map(|i| ReadJob {
                    offset: (i * 4096) as u64,
                    dst: arena.slot_ptr(i),
                    len: 4096,
                    tag: i,
                })
                .collect(),
        )
        .unwrap();
    reader
        .drain(|tag| {
            // SAFETY: the completed read owns a valid arena slot; the shared destination
            // remains alive through the synchronous copy.
            unsafe {
                gpu.copy_to_device(
                    &queue,
                    address + (tag * 4096) as u64,
                    arena.slot_ptr(tag),
                    4096,
                )
            }
        })
        .unwrap();
    let event = gpu.record_copies(&queue).unwrap();
    gpu.compute_wait(&event).unwrap();
    assert_eq!(gpu.download_bytes(&destination).unwrap(), bytes);
    // SAFETY: deliberate invalid destination; validation rejects it before reading src.
    assert!(unsafe { gpu.copy_to_device(&queue, address + 8191, arena.slot_ptr(0), 2) }.is_err());
}
