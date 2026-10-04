//! Layer-major prefill must equal running the same chunks one after another through
//! the whole model. Needs a GPU and a converted model:
//!
//!     OOMINF_MODEL=/path/model.oom cargo test --release -p oominf-models-qwen -- --ignored

use std::sync::Arc;

use oominf_core::{Memory, NoProbe};
use oominf_cuda::Gpu;
use oominf_models_qwen::{Dims, QwenModel};
use oominf_tiers::{TieredExperts, policy};

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
    let model = QwenModel::load(&*gpu, &files, dims, None).unwrap();
    let layout = "nvfp4-modelopt-g16";
    let vram = oominf_tiers::slots_for(&files, layout, 3.0);
    let host = oominf_tiers::slots_for(&files, layout, 6.0);
    let mut experts = TieredExperts::new(
        gpu.clone(),
        files.clone(),
        layout,
        vram,
        host,
        policy::parse("lru").unwrap(),
        policy::parse("lru").unwrap(),
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
    let differing = step_major
        .iter()
        .zip(&layer_major)
        .filter(|(x, y)| x.to_bits() != y.to_bits())
        .count();
    assert_eq!(differing, 0, "{differing} logits differ");
    assert_eq!(a.pos, b.pos);
}
