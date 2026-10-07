//! Emit full-model logits for independent reference comparisons.
#[cfg(target_os = "macos")]
fn main() -> anyhow::Result<()> {
    use oominf_tiers::policy::Lru;
    use std::path::Path;
    use std::sync::Arc;
    let args: Vec<_> = std::env::args().collect();
    anyhow::ensure!(
        args.len() == 4,
        "usage: logits MODEL_DIR OUT_DIR TOKEN,TOKEN,..."
    );
    let tokens: Vec<u32> = args[3]
        .split(',')
        .map(str::parse)
        .collect::<Result<_, _>>()?;
    std::fs::create_dir_all(&args[2])?;
    let model = oominf_models_qwen35::open(
        Arc::new(oominf_format::Model::open(Path::new(&args[1]))?),
        oominf_models_qwen35::Options {
            max_context: tokens.len(),
            reserve_bytes: 1 << 30,
            cache_bytes: Some(256 << 20),
            io: Default::default(),
            device_policy: Box::new(Lru::default()),
            host_policy: Box::new(Lru::default()),
            disk_only: false,
        },
    )?;
    let mut session = model.new_session(tokens.len())?;
    for (i, token) in tokens.iter().enumerate() {
        let logits = session.step(&[*token])?;
        let bytes: Vec<_> = logits.iter().flat_map(|v| v.to_le_bytes()).collect();
        std::fs::write(Path::new(&args[2]).join(format!("{i}.f32")), bytes)?;
        println!(
            "step {i}: token {token}, top1 {}",
            oominf_core::argmax(&logits)
        );
    }
    Ok(())
}

#[cfg(not(target_os = "macos"))]
fn main() {
    eprintln!("the Metal logit fixture requires macOS");
}
