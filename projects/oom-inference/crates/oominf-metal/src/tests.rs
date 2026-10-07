use super::Gpu;
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
