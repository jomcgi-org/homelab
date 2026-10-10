//! Layer-major prefill must match running the same chunks one after another through
//! the model, up to floating-point summation order. Checked on the first layer:
//! across many layers discrete expert routing amplifies rounding-level differences
//! (1 layer 6e-6, 4 layers 3e-3, 48 layers ~0.1 on random tokens), so a deep
//! comparison cannot separate a bug from rounding, while a real bug shows at once. Needs a GPU and a converted model:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen -- --ignored

use std::sync::Arc;

use oominf_core::{Memory, NoProbe};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, QwenModel};
use oominf_tiers::{TierSizes, TieredExperts, policy};

#[test]
#[ignore = "needs a GPU and OOMINF_MODEL"]
fn layer_major_prefill_matches_chunked_forward() {
    let dir = std::path::PathBuf::from(
        std::env::var_os("OOMINF_MODEL").expect("set OOMINF_MODEL to a converted model"),
    );
    let files = Arc::new(oominf_format::Model::open(&dir).unwrap());
    let dims =
        Dims::from_config(&std::fs::read_to_string(dir.join("config.json")).unwrap()).unwrap();
    let gpu = Arc::new(Gpu::new(0).unwrap());
    let model = QwenModel::load(&*gpu, &files, dims, Some(1)).unwrap();
    let layout = "nvfp4-modelopt-g16";
    let vram = oominf_tiers::slots_for(&files, layout, 3.0);
    let host = oominf_tiers::slots_for(&files, layout, 6.0);
    let mut experts = TieredExperts::new(
        gpu.clone(),
        files.clone(),
        layout,
        TierSizes {
            vram_slots: vram,
            host_slots: host,
            host_stage_slots: 512,
            max_fetch: 512,
        },
        policy::parse("lru").unwrap(),
        policy::parse("lru").unwrap(),
        &oominf_tiers::host::IoConfig::default(),
    )
    .unwrap();

    let ids: Vec<u32> = (0..200u32).map(|i| 1000 + (i * 7919) % 50_000).collect();
    let chunk = 64;

    let mut a = model.new_state(&*gpu, ids.len()).unwrap();
    let mut step_major = None;
    for c in ids.chunks(chunk) {
        step_major = Some(
            model
                .forward(&*gpu, c, &mut a, &mut experts, &mut NoProbe, true)
                .unwrap(),
        );
    }
    let step_major = gpu.download_f32(&step_major.unwrap()).unwrap();

    let mut b = model.new_state(&*gpu, ids.len()).unwrap();
    let layer_major = model
        .prefill(&*gpu, &ids, chunk, &mut b, &mut experts, &|| false)
        .unwrap()
        .unwrap();
    let layer_major = gpu.download_f32(&layer_major).unwrap();

    assert_eq!(step_major.len(), layer_major.len());
    // Routing a fetch group and running its experts as one step changes floating-
    // point summation order (GEMM shapes, kernel choice), not the computation: the
    // logits must agree to fp32 rounding and pick the same token.
    let (err, norm) = step_major
        .iter()
        .zip(&layer_major)
        .fold((0f64, 0f64), |(e, n), (&x, &y)| {
            (e + (x as f64 - y as f64).powi(2), n + (x as f64).powi(2))
        });
    let rms_rel = (err / norm).sqrt();
    println!("layer-major vs step-major logits: rms relative difference {rms_rel:.3e}");
    assert!(rms_rel < 1e-4, "logits differ by {rms_rel:.3e}");
    assert_eq!(
        oominf_core::argmax(&step_major),
        oominf_core::argmax(&layer_major)
    );
    assert_eq!(a.pos, b.pos);
}
