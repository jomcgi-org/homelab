//! `oominf serve`: OpenAI- and Anthropic-compatible HTTP API over one engine thread.
//!
//! Requests are rendered with the model's chat template, tokenized, and queued to
//! the engine, which serves them one at a time and reuses the previous sequence's
//! state when a prompt extends it. Generated tokens are detokenized and parsed
//! into reasoning, content and tool calls on the async side.

pub mod anthropic;
pub mod engine;
pub mod generation;
pub mod openai;
pub mod parse;
pub mod sampling;
pub mod template;

use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use anyhow::{Context, Result};
use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};

use crate::engine::{EngineConfig, EngineHandle, ExpertFactory};
use crate::sampling::SamplingParams;
use crate::template::ChatTemplate;

pub struct ServeConfig {
    pub engine: EngineConfig,
    pub addr: SocketAddr,
    pub model_name: String,
}

/// Shared state of the HTTP handlers.
pub struct App {
    pub engine: EngineHandle,
    pub template: ChatTemplate,
    pub model_name: String,
    pub max_context: usize,
    /// Sampling defaults from the model's `generation_config.json`.
    pub defaults: SamplingParams,
    /// Token ids that end a turn.
    pub stop_ids: Vec<u32>,
    pub started: u64,
    ready: AtomicBool,
    ids: AtomicU64,
}

impl App {
    /// An app that is not ready until [`App::set_ready`] is called.
    pub fn new(
        engine: EngineHandle,
        template: ChatTemplate,
        model_name: String,
        max_context: usize,
        defaults: SamplingParams,
        stop_ids: Vec<u32>,
    ) -> Self {
        App {
            engine,
            template,
            model_name,
            max_context,
            defaults,
            stop_ids,
            started: unix_now(),
            ready: AtomicBool::new(false),
            ids: AtomicU64::new(0),
        }
    }

    pub fn set_ready(&self) {
        self.ready.store(true, Ordering::Release);
    }

    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::Acquire)
    }

    /// A unique id with the given prefix, e.g. `chatcmpl-1a`.
    pub fn new_id(&self, prefix: &str) -> String {
        format!(
            "{prefix}-{:x}{:x}",
            self.started,
            self.ids.fetch_add(1, Ordering::Relaxed)
        )
    }
}

pub fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

pub fn validate_sampling(p: &SamplingParams) -> Result<(), String> {
    if !(0.0..=2.0).contains(&p.temperature) {
        return Err("temperature must be in [0, 2]".into());
    }
    if !(p.top_p > 0.0 && p.top_p <= 1.0) {
        return Err("top_p must be in (0, 1]".into());
    }
    Ok(())
}

/// Sampling defaults and stop tokens from `generation_config.json`.
fn generation_defaults(
    model_dir: &std::path::Path,
    template: &ChatTemplate,
) -> Result<(SamplingParams, Vec<u32>)> {
    let path = model_dir.join("generation_config.json");
    let cfg: serde_json::Value = match std::fs::read(&path) {
        Ok(b) => serde_json::from_slice(&b).with_context(|| path.display().to_string())?,
        Err(_) => serde_json::Value::Null,
    };
    let f = |k: &str| cfg.get(k).and_then(serde_json::Value::as_f64);
    let defaults = SamplingParams {
        temperature: f("temperature").unwrap_or(1.0) as f32,
        top_p: f("top_p").unwrap_or(1.0) as f32,
        top_k: f("top_k").unwrap_or(0.0) as usize,
        ..SamplingParams::default()
    };
    let mut stop_ids: Vec<u32> = match cfg.get("eos_token_id") {
        Some(serde_json::Value::Array(a)) => a
            .iter()
            .filter_map(|v| v.as_u64())
            .map(|v| v as u32)
            .collect(),
        Some(v) => v.as_u64().map(|v| vec![v as u32]).unwrap_or_default(),
        None => Vec::new(),
    };
    if let Some(id) = template
        .token_id("<|im_end|>")
        .filter(|id| !stop_ids.contains(id))
    {
        stop_ids.push(id);
    }
    anyhow::ensure!(!stop_ids.is_empty(), "no end-of-turn token ids found");
    Ok((defaults, stop_ids))
}

pub fn router(app: Arc<App>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/v1/models", get(openai::models))
        .route("/v1/chat/completions", post(openai::chat_completions))
        .route("/v1/messages", post(anthropic::messages))
        .with_state(app)
}

async fn health(State(app): State<Arc<App>>) -> Response {
    if app.is_ready() {
        (StatusCode::OK, "ok").into_response()
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "loading").into_response()
    }
}

/// Builds the app and starts the engine; the returned app turns ready once the
/// model has loaded. Load failures are returned through the join handle.
pub fn build(
    engine_cfg: EngineConfig,
    model_name: String,
    experts: ExpertFactory,
) -> Result<(Arc<App>, tokio::task::JoinHandle<Result<String>>)> {
    let template = ChatTemplate::load(&engine_cfg.model_dir)?;
    let (defaults, stop_ids) = generation_defaults(&engine_cfg.model_dir, &template)?;
    let max_context = engine_cfg.max_context;
    let (engine, ready) = engine::start(engine_cfg, experts);
    let app = Arc::new(App::new(
        engine,
        template,
        model_name,
        max_context,
        defaults,
        stop_ids,
    ));
    let waiter = app.clone();
    let loaded = tokio::task::spawn_blocking(move || {
        let summary = ready.recv().context("engine thread exited during load")??;
        waiter.set_ready();
        Ok(summary)
    });
    Ok((app, loaded))
}

/// Serves until interrupted.
pub fn serve(cfg: ServeConfig, experts: ExpertFactory) -> Result<()> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    rt.block_on(async move {
        let (app, loaded) = build(cfg.engine, cfg.model_name, experts)?;
        let listener = tokio::net::TcpListener::bind(cfg.addr)
            .await
            .with_context(|| format!("bind {}", cfg.addr))?;
        eprintln!("oominf: listening on http://{} (loading model)", cfg.addr);
        tokio::spawn(async move {
            match loaded.await {
                Ok(Ok(summary)) => eprintln!("oominf: ready; {summary}"),
                Ok(Err(e)) => {
                    eprintln!("oominf: model load failed: {e:#}");
                    std::process::exit(1);
                }
                Err(e) => {
                    eprintln!("oominf: model load task failed: {e}");
                    std::process::exit(1);
                }
            }
        });
        axum::serve(listener, router(app))
            .with_graceful_shutdown(async {
                let _ = tokio::signal::ctrl_c().await;
            })
            .await?;
        Ok(())
    })
}

/// Default name to serve a model directory under.
pub fn default_model_name(model_dir: &std::path::Path) -> String {
    let name = model_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("model");
    name.strip_suffix(".oom").unwrap_or(name).to_owned()
}
