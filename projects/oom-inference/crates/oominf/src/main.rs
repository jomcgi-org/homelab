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
