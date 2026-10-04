mod chat;
mod check_layer;
mod check_model;
mod generate;

use std::path::PathBuf;
use std::time::Instant;

use anyhow::{Result, bail};
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "oominf",
    about = "oom-inference: MoE inference for models bigger than your memory"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Convert an upstream release checkpoint into the oominf weight format.
    Convert {
        /// Upstream checkpoint directory (sharded safetensors).
        #[arg(long)]
        src: PathBuf,
        /// Output directory (must not already hold a converted model).
        #[arg(long)]
        out: PathBuf,
        /// Only convert routed experts for these layers, e.g. `0-3` or `5`.
        #[arg(long)]
        layers: Option<String>,
        /// Skip gather tables.
        #[arg(long)]
        no_tables: bool,
    },
    /// Recompute every checksum in a converted model.
    Verify { model: PathBuf },
    /// Summarise a converted model.
    Inspect { model: PathBuf },
    /// Run the whole model on the GPU against the reference whole-model chain.
    CheckModel {
        #[arg(long)]
        model: PathBuf,
        /// Fixture directory of the chain (holds manifest.json, fp32/, bf16/).
        #[arg(long)]
        fixtures: PathBuf,
    },
    /// Greedy-decode one chat turn (correctness tool).
    Generate {
        #[arg(long)]
        model: PathBuf,
        #[arg(long)]
        prompt: String,
        #[arg(long, default_value_t = 64)]
        max_tokens: usize,
    },
    /// Render a user turn with the chat template and tokenize it; with
    /// `--manifest`, check it against a reference fixture manifest.
    Tokenize {
        #[arg(long)]
        model: PathBuf,
        #[arg(long)]
        prompt: Option<String>,
        #[arg(long)]
        manifest: Option<PathBuf>,
    },
    /// Run one decoder layer on the GPU against reference fixtures.
    CheckLayer {
        /// Converted model directory.
        #[arg(long)]
        model: PathBuf,
        /// Fixture directory for the layer (holds tolerances.json and <mode>/fp32/).
        #[arg(long)]
        fixtures: PathBuf,
        #[arg(long, default_value_t = 0)]
        layer: u32,
        /// Expert activation mode of the fixtures to check against.
        #[arg(long, default_value = "w4a16")]
        mode: String,
    },
}

fn parse_layers(s: &str) -> Result<std::ops::RangeInclusive<u32>> {
    Ok(match s.split_once('-') {
        Some((a, b)) => a.parse()?..=b.parse()?,
        None => {
            let n = s.parse()?;
            n..=n
        }
    })
}

fn gib(b: u64) -> f64 {
    b as f64 / (1u64 << 30) as f64
}

fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Convert {
            src,
            out,
            layers,
            no_tables,
        } => {
            let opts = oominf_convert::Options {
                expert_layers: layers.as_deref().map(parse_layers).transpose()?,
                tables: !no_tables,
            };
            let start = Instant::now();
            let (index, s) = oominf_convert::convert(&src, &out, &opts, |m| {
                eprintln!("[{:>6.1}s] {m}", start.elapsed().as_secs_f64())
            })?;
            let f = &index.files;
            println!(
                "converted in {:.1}s: {} dense ({:.2} GiB), {} expert layers ({:.2} GiB), {} tables ({:.2} GiB); {} skipped, {} excluded",
                start.elapsed().as_secs_f64(),
                s.dense,
                gib(f.dense.bytes),
                s.expert_layers,
                gib(f.experts.bytes),
                s.tables,
                gib(f.tables.bytes),
                s.skipped,
                s.excluded
            );
        }
        Command::Verify { model } => {
            let m = oominf_format::Model::open(&model)?;
            let start = Instant::now();
            let mut last = 0u64;
            let r = m.verify(|done| {
                if done - last > 8 << 30 {
                    last = done;
                    eprintln!("  {:.1} GiB checked", gib(done));
                }
            })?;
            println!(
                "{} tensors, {} records, {:.2} GiB in {:.1}s: {} mismatches",
                r.tensors_checked,
                r.records_checked,
                gib(r.bytes_checked),
                start.elapsed().as_secs_f64(),
                r.mismatches.len()
            );
            for m in &r.mismatches {
                println!("  MISMATCH {m}");
            }
            if !r.mismatches.is_empty() {
                bail!("checksum verification failed");
            }
        }
        Command::CheckLayer {
            model,
            fixtures,
            layer,
            mode,
        } => {
            if !check_layer::run(&model, &fixtures, layer, &mode)? {
                bail!("isolated stages over budget");
            }
        }
        Command::CheckModel { model, fixtures } => {
            if !check_model::run(&model, &fixtures)? {
                bail!("logits less faithful than the reference's bf16 run");
            }
        }
        Command::Generate {
            model,
            prompt,
            max_tokens,
        } => generate::run(&model, &prompt, max_tokens)?,
        Command::Tokenize {
            model,
            prompt,
            manifest,
        } => {
            let chat = chat::Chat::load(&model)?;
            let m: Option<serde_json::Value> = manifest
                .map(|p| -> Result<_> { Ok(serde_json::from_slice(&std::fs::read(p)?)?) })
                .transpose()?;
            let user = match (&prompt, &m) {
                (Some(p), _) => p.clone(),
                (None, Some(m)) => m["prompt"]["user_message"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                (None, None) => bail!("give --prompt or --manifest"),
            };
            let text = chat.render_user(&user)?;
            let ids = chat.encode(&text)?;
            println!("{} tokens: {ids:?}", ids.len());
            if let Some(m) = m {
                let want_text = m["prompt"]["templated"].as_str().unwrap_or_default();
                let want: Vec<u32> = m["prompt"]["token_ids"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_u64())
                            .map(|v| v as u32)
                            .collect()
                    })
                    .unwrap_or_default();
                println!(
                    "template matches manifest: {}; token ids match: {}",
                    text == want_text,
                    ids == want
                );
                if text != want_text || ids != want {
                    bail!("tokenization differs from the reference");
                }
            }
        }
        Command::Inspect { model } => {
            let m = oominf_format::Model::open(&model)?;
            let i = m.index();
            println!(
                "{} v{} from {} ({}, {})",
                i.format, i.version, i.source.path, i.source.model_type, i.source.fingerprint
            );
            println!(
                "files: dense {:.2} GiB, experts {:.2} GiB, tables {:.2} GiB; {} tensors",
                gib(i.files.dense.bytes),
                gib(i.files.experts.bytes),
                gib(i.files.tables.bytes),
                i.tensors.len()
            );
            for g in &i.expert_groups {
                println!(
                    "layer {:>3}: {} experts x {} bytes, layout {}",
                    g.layer, g.num_experts, g.schema.stride, g.schema.layout
                );
            }
            if let Some(g) = i.expert_groups.first() {
                for p in &g.schema.parts {
                    println!(
                        "  part {:<20} {:<8} {:?} @{} ({} bytes)",
                        p.name, p.dtype, p.shape, p.offset, p.nbytes
                    );
                }
            }
        }
    }
    Ok(())
}
