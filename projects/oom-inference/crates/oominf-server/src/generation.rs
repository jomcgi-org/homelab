//! Turns a chat request into engine tokens and back into parsed output, shared by
//! every API flavour.

use std::sync::Arc;

use serde_json::Value;
use tokio::sync::mpsc;

use crate::App;
use crate::engine::{Event, FinishReason, Job, SubmitError};
use crate::parse::{OutputParser, Piece, StopMatcher, ToolSchemas};
use crate::sampling::SamplingParams;
use crate::template::{Detokenizer, TemplateOptions};

/// A chat request in the template's message shape.
#[derive(Debug, Clone)]
pub struct GenRequest {
    /// OpenAI-shaped messages; assistant `tool_calls[].function.arguments` are objects.
    pub messages: Vec<Value>,
    /// OpenAI-shaped tool definitions (`{"type": "function", "function": {...}}`).
    pub tools: Vec<Value>,
    pub template: TemplateOptions,
    pub sampling: SamplingParams,
    pub max_tokens: usize,
    pub stop: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Finish {
    /// The model ended its turn.
    EndTurn,
    /// The turn ended with tool calls.
    ToolCalls,
    /// A caller-supplied stop sequence matched.
    StopSequence,
    /// `max_tokens` or the context limit was reached.
    Length,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Usage {
    pub prompt_tokens: usize,
    pub completion_tokens: usize,
    /// Prompt tokens served from the cached sequence.
    pub cached_tokens: usize,
}

#[derive(Debug)]
pub enum Output {
    Piece(Piece),
    Done(Finish, Usage),
    Failed(String),
}

/// Why a request was not started.
#[derive(Debug)]
pub enum Rejected {
    /// The request itself is invalid (400).
    BadRequest(String),
    /// Too many requests are waiting; retry after this many seconds (429).
    Busy(u64),
    /// The engine is gone (500).
    Stopped,
}

/// Seconds a refused client is asked to wait before retrying: about one short
/// request's time.
pub const RETRY_AFTER_SECS: u64 = 5;

use Rejected::BadRequest;

/// Renders and tokenizes `req`, submits it, and returns a stream of parsed output.
/// Dropping the receiver cancels generation.
pub fn start(app: &Arc<App>, req: GenRequest) -> Result<mpsc::Receiver<Output>, Rejected> {
    let tools = (!req.tools.is_empty()).then_some(req.tools.as_slice());
    let text = app
        .template
        .render(&req.messages, tools, &req.template)
        .map_err(|e| BadRequest(format!("cannot render chat template: {e:#}")))?;
    let prompt = app
        .template
        .encode(&text)
        .map_err(|e| BadRequest(format!("{e:#}")))?;
    if prompt.len() >= app.max_context {
        return Err(BadRequest(format!(
            "prompt is {} tokens; the context limit is {}",
            prompt.len(),
            app.max_context
        )));
    }
    let schemas: ToolSchemas = req
        .tools
        .iter()
        .filter_map(|t| {
            let f = t.get("function")?;
            Some((
                f.get("name")?.as_str()?.to_owned(),
                f.get("parameters").cloned().unwrap_or_default(),
            ))
        })
        .collect();
    let reuse_at = app
        .template
        .reuse_points(&req.messages, tools, &req.template, &text, &prompt);
    let mut parser: Box<dyn OutputParser> = (app.parser)(&text, schemas);

    let (ev_tx, mut ev_rx) = mpsc::channel(256);
    app.engine
        .submit(Job {
            prompt,
            reuse_at,
            sampling: req.sampling,
            max_tokens: req.max_tokens,
            stop_ids: app.stop_ids.clone(),
            events: ev_tx,
            ticket: None,
        })
        .map_err(|e| match e {
            SubmitError::Busy => Rejected::Busy(RETRY_AFTER_SECS),
            SubmitError::Stopped => Rejected::Stopped,
        })?;

    let (out_tx, out_rx) = mpsc::channel(256);
    let app = app.clone();
    let mut stops = StopMatcher::new(req.stop);
    tokio::spawn(async move {
        let mut detok = Detokenizer::new();
        let mut usage = Usage::default();
        let mut tool_calls = false;
        let mut pieces = Vec::new();
        let finish = loop {
            let Some(event) = ev_rx.recv().await else {
                let _ = out_tx.send(Output::Failed("engine stopped".into())).await;
                return;
            };
            match event {
                Event::Started {
                    prompt_tokens,
                    cached,
                } => {
                    usage.prompt_tokens = prompt_tokens;
                    usage.cached_tokens = cached;
                }
                Event::Token(id) => {
                    usage.completion_tokens += 1;
                    let text = match detok.push(&app.template, id) {
                        Ok(t) => t,
                        Err(e) => {
                            let _ = out_tx.send(Output::Failed(format!("{e:#}"))).await;
                            return;
                        }
                    };
                    let (text, hit) = stops.push(&text);
                    parser.push(&text, &mut pieces);
                    if hit {
                        break Finish::StopSequence;
                    }
                }
                Event::Finished(reason) => {
                    let rest = stops.finish();
                    parser.push(&rest, &mut pieces);
                    break match reason {
                        FinishReason::Stop => Finish::EndTurn,
                        FinishReason::Length => Finish::Length,
                    };
                }
                Event::Failed(e) => {
                    let _ = out_tx.send(Output::Failed(e)).await;
                    return;
                }
            }
            for p in pieces.drain(..) {
                tool_calls |= matches!(p, Piece::ToolCall(_));
                if out_tx.send(Output::Piece(p)).await.is_err() {
                    return; // client gone: dropping ev_rx cancels the engine
                }
            }
        };
        parser.finish(&mut pieces);
        for p in pieces.drain(..) {
            tool_calls |= matches!(p, Piece::ToolCall(_));
            let _ = out_tx.send(Output::Piece(p)).await;
        }
        let finish = if tool_calls && finish == Finish::EndTurn {
            Finish::ToolCalls
        } else {
            finish
        };
        let _ = out_tx.send(Output::Done(finish, usage)).await;
    });
    Ok(out_rx)
}

/// Collected output of a non-streaming request.
#[derive(Debug, Default)]
pub struct Collected {
    pub reasoning: String,
    pub content: String,
    pub tool_calls: Vec<crate::parse::ToolCall>,
    pub finish: Option<Finish>,
    pub usage: Usage,
}

pub async fn collect(mut rx: mpsc::Receiver<Output>) -> Result<Collected, String> {
    let mut c = Collected::default();
    while let Some(out) = rx.recv().await {
        match out {
            Output::Piece(Piece::Reasoning(s)) => c.reasoning.push_str(&s),
            Output::Piece(Piece::Content(s)) => c.content.push_str(&s),
            Output::Piece(Piece::ToolCall(t)) => c.tool_calls.push(t),
            Output::Done(f, u) => {
                c.finish = Some(f);
                c.usage = u;
            }
            Output::Failed(e) => return Err(e),
        }
    }
    Ok(c)
}
