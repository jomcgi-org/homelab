mod bench;
mod chat;
mod check_layer;
mod check_model;
mod generate;
mod load;
mod score;

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
        #[command(flatten)]
        experts: load::ExpertArgs,
        #[command(flatten)]
        cache: load::CacheArgs,
    },
    /// Greedy decode with expert-source statistics (performance baseline).
    Bench {
        #[arg(long)]
        model: PathBuf,
        #[arg(long, default_value = "Write a short story about a lighthouse keeper.")]
        prompt: String,
        /// Read the prompt from a file instead (e.g. a long document).
        #[arg(long)]
        prompt_file: Option<PathBuf>,
        #[arg(long, default_value_t = 32)]
        tokens: usize,
        /// Prompt tokens per prefill chunk (default: the model family's).
        #[arg(long)]
        prefill_chunk: Option<usize>,
        /// Draft tokens per decode step (speculative decoding); 0 disables it.
        #[arg(long, default_value_t = 1)]
        draft: usize,
        /// Tokens per prompt-lookup draft (0: the model's drafts only).
        #[arg(long, default_value_t = 0)]
        prompt_lookup: usize,
        /// After decoding, time verification steps of these widths (comma
        /// separated): the cost of checking prompt-lookup drafts.
        #[arg(long, value_delimiter = ',')]
        verify: Vec<usize>,
        /// After decoding, time batched steps over this many concurrent sequences
        /// (comma separated), each fed one token per step.
        #[arg(long, value_delimiter = ',')]
        streams: Vec<usize>,
        #[command(flatten)]
        experts: load::ExpertArgs,
        #[command(flatten)]
        cache: load::CacheArgs,
    },
    /// Teacher-forced next-token distributions over a long document's tail, written
    /// for a later comparison or compared with a reference run (precision modes).
    Score {
        #[arg(long)]
        model: PathBuf,
        /// The document (rendered as one user message).
        #[arg(long)]
        prompt_file: PathBuf,
        /// Tokens at the end of the document to score.
        #[arg(long, default_value_t = 256)]
        tail: usize,
        /// Write each scored position's logits here.
        #[arg(long)]
        out: Option<PathBuf>,
        /// Compare with logits written by an earlier run.
        #[arg(long)]
        against: Option<PathBuf>,
        /// Prompt tokens per prefill chunk (default: the model family's).
        #[arg(long)]
        prefill_chunk: Option<usize>,
        #[command(flatten)]
        experts: load::ExpertArgs,
        #[command(flatten)]
        cache: load::CacheArgs,
    },
    /// Serve the model over OpenAI- and Anthropic-compatible HTTP APIs.
    Serve {
        #[arg(long)]
        model: PathBuf,
        #[arg(long, default_value = "127.0.0.1")]
        host: std::net::IpAddr,
        #[arg(long, default_value_t = 8091)]
        port: u16,
        /// Model name reported by the API (default: the model directory's name).
        #[arg(long)]
        served_model_name: Option<String>,
        /// Longest sequence (prompt plus generation) the KV cache is sized for.
        #[arg(long, default_value_t = 32768)]
        max_context: usize,
        /// Prompt tokens per prefill chunk (bounds prefill activation memory; default:
        /// the model family's).
        #[arg(long)]
        prefill_chunk: Option<usize>,
        /// Draft tokens per decode step (speculative decoding); 0 disables it.
        #[arg(long, default_value_t = 1)]
        draft: usize,
        /// Tokens per prompt-lookup draft: when the latest tokens repeat earlier
        /// ones (code being edited, quoted input), what followed them is drafted
        /// instead of the model's draft. On by default: +22-40% output rate when
        /// output copies input (file edits), -2 to -9% on prose and short diffs
        /// (#6872). 0 disables it.
        #[arg(long, default_value_t = 7)]
        prompt_lookup: usize,
        /// Save sequences evicted from the device here, and resume later requests
        /// that extend one instead of prefilling (off when unset).
        #[arg(long)]
        prefix_store_dir: Option<PathBuf>,
        /// Disk budget of the prefix store.
        #[arg(long, default_value_t = 200.0)]
        prefix_store_gib: f64,
        /// Prefix store entries unused for longer are deleted.
        #[arg(long, default_value_t = 72.0)]
        prefix_store_ttl_hours: f64,
        /// Shorter sequences are not saved (they prefill in moments).
        #[arg(long, default_value_t = 1024)]
        prefix_store_min_tokens: usize,
        /// Requests decoding at once (continuous batching: their tokens share each
        /// step). 1 serves one request at a time, to completion.
        #[arg(long, default_value_t = 1)]
        max_streams: usize,
        /// Most tokens a batched step carries (every stream's next token plus the
        /// drafts the token budget picks).
        #[arg(long, default_value_t = oominf_server::engine::MAX_STEP_TOKENS)]
        max_step_tokens: usize,
        /// Step cost in milliseconds by width, `width:ms` points (interpolated,
        /// then refined by measured steps): what the token budget trades drafts
        /// against.
        #[arg(long, default_value = oominf_server::budget::DEFAULT_STEP_COST)]
        step_cost: String,
        /// Prompt tokens prefilled at a time while other requests decode (0: whole
        /// prompts; other requests wait for the prefill).
        #[arg(long, default_value_t = 0)]
        prefill_slice: usize,
        #[command(flatten)]
        experts: load::ExpertArgs,
        #[command(flatten)]
        cache: load::CacheArgs,
    },
    /// Greedy-decode one chat turn (correctness tool).
    Generate {
        #[arg(long)]
        model: PathBuf,
        #[arg(long, required_unless_present = "prompt_file")]
        prompt: Option<String>,
        /// Read the prompt from a file instead (e.g. a long document).
        #[arg(long, conflicts_with = "prompt")]
        prompt_file: Option<PathBuf>,
        #[arg(long, default_value_t = 64)]
        max_tokens: usize,
        /// Draft tokens per decode step (speculative decoding); 0 disables it.
        #[arg(long, default_value_t = 1)]
        draft: usize,
        #[command(flatten)]
        experts: load::ExpertArgs,
        #[command(flatten)]
        cache: load::CacheArgs,
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
        Command::CheckModel {
            model,
            fixtures,
            experts,
            cache,
        } => {
            if !check_model::run(&model, &fixtures, &experts, &cache)? {
                bail!("logits less faithful than the reference's bf16 run");
            }
        }
        Command::Bench {
            model,
            prompt,
            prompt_file,
            tokens,
            prefill_chunk,
            draft,
            prompt_lookup,
            verify,
            streams,
            experts,
            cache,
        } => {
            let prompt = match prompt_file {
                Some(p) => std::fs::read_to_string(p)?,
                None => prompt,
            };
            bench::run(
                &model,
                &prompt,
                tokens,
                prefill_chunk,
                draft,
                prompt_lookup,
                &verify,
                &streams,
                &experts,
                &cache,
            )?
        }
        Command::Score {
            model,
            prompt_file,
            tail,
            out,
            against,
            prefill_chunk,
            experts,
            cache,
        } => score::run(
            &model,
            &std::fs::read_to_string(prompt_file)?,
            &score::Options {
                tail,
                out: out.as_deref(),
                against: against.as_deref(),
                prefill_chunk,
            },
            &experts,
            &cache,
        )?,
        Command::Serve {
            model,
            host,
            port,
            served_model_name,
            max_context,
            prefill_chunk,
            draft,
            prompt_lookup,
            prefix_store_dir,
            prefix_store_gib,
            prefix_store_ttl_hours,
            prefix_store_min_tokens,
            max_streams,
            max_step_tokens,
            step_cost,
            prefill_slice,
            experts,
            cache,
        } => {
            anyhow::ensure!(
                max_streams >= 1 && max_streams <= max_step_tokens,
                "--max-streams must be between 1 and --max-step-tokens"
            );
            let schedule = oominf_server::engine::Schedule {
                max_streams,
                step_cost: oominf_server::budget::CostCurve::parse(&step_cost, max_step_tokens)?,
                draft_ms: oominf_server::engine::DEFAULT_DRAFT_MS,
                prefill_slice,
            };
            let prefix_store = match prefix_store_dir {
                Some(dir) => Some(oominf_server::store::StoreConfig {
                    dir,
                    budget_bytes: (prefix_store_gib * (1u64 << 30) as f64) as u64,
                    ttl: std::time::Duration::from_secs_f64(prefix_store_ttl_hours * 3600.0),
                    min_tokens: prefix_store_min_tokens,
                    identity: format!(
                        "{} kv={:?} dense={:?} experts={:?} attention={:?}",
                        load::checkpoint_id(&model)?,
                        cache.kv_cache,
                        cache.dense,
                        cache.expert_precision,
                        cache.attention_precision
                    ),
                }),
                None => None,
            };
            let model_name =
                served_model_name.unwrap_or_else(|| oominf_server::default_model_name(&model));
            let model_type = oominf_models::model_type(&oominf_format::Model::open(&model)?)?;
            let cfg = oominf_server::ServeConfig {
                model_dir: model.clone(),
                model_type,
                max_context,
                draft,
                prompt_lookup,
                addr: std::net::SocketAddr::new(host, port),
                model_name,
                prefix_store,
                schedule,
            };
            oominf_server::serve(
                cfg,
                Box::new(move || {
                    load::open_model(&load::OpenArgs {
                        model_dir: &model,
                        max_context,
                        prefill_chunk,
                        experts: &experts,
                        cache: &cache,
                    })
                }),
            )?
        }
        Command::Generate {
            model,
            prompt,
            prompt_file,
            max_tokens,
            draft,
            experts,
            cache,
        } => {
            let prompt = match prompt_file {
                Some(p) => std::fs::read_to_string(p)?,
                None => prompt.unwrap_or_default(),
            };
            generate::run(&model, &prompt, max_tokens, draft, &experts, &cache)?
        }
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
