//! Anthropic-compatible `/v1/messages`.

use std::convert::Infallible;
use std::sync::Arc;

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio_stream::wrappers::ReceiverStream;

use crate::App;
use crate::generation::{self, BadRequest, Finish, GenRequest, Output, Usage};
use crate::parse::Piece;
use crate::template::TemplateOptions;

#[derive(Debug, Deserialize)]
pub struct MessagesRequest {
    pub max_tokens: usize,
    pub messages: Vec<Message>,
    pub system: Option<Value>,
    #[serde(default)]
    pub tools: Vec<Tool>,
    pub tool_choice: Option<Value>,
    #[serde(default)]
    pub stop_sequences: Vec<String>,
    pub temperature: Option<f32>,
    pub top_p: Option<f32>,
    pub top_k: Option<usize>,
    #[serde(default)]
    pub stream: bool,
    pub thinking: Option<Thinking>,
}

#[derive(Debug, Deserialize)]
pub struct Message {
    pub role: String,
    pub content: Value,
}

#[derive(Debug, Deserialize)]
pub struct Tool {
    pub name: String,
    #[serde(default)]
    pub description: String,
    pub input_schema: Value,
}

#[derive(Debug, Deserialize)]
pub struct Thinking {
    #[serde(rename = "type")]
    pub kind: String,
}

pub fn error(status: StatusCode, message: &str) -> Response {
    let kind = match status {
        StatusCode::SERVICE_UNAVAILABLE => "overloaded_error",
        s if s.is_client_error() => "invalid_request_error",
        _ => "api_error",
    };
    (
        status,
        Json(json!({"type": "error", "error": {"type": kind, "message": message}})),
    )
        .into_response()
}

/// Concatenates the text of a string or a list of `text` blocks.
fn text_of(content: &Value) -> Result<String, String> {
    match content {
        Value::String(s) => Ok(s.clone()),
        Value::Array(blocks) => blocks
            .iter()
            .map(|b| match b.get("type").and_then(Value::as_str) {
                Some("text") => Ok(b
                    .get("text")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_owned()),
                other => Err(format!("unsupported content block {other:?} here")),
            })
            .collect::<Result<Vec<_>, _>>()
            .map(|v| v.join("")),
        Value::Null => Ok(String::new()),
        _ => Err("content must be a string or a list of blocks".into()),
    }
}

/// Converts Anthropic messages into the template's OpenAI-shaped messages.
pub fn to_messages(system: Option<&Value>, messages: Vec<Message>) -> Result<Vec<Value>, String> {
    let mut out = Vec::new();
    if let Some(s) = system {
        let text = text_of(s)?;
        if !text.is_empty() {
            out.push(json!({"role": "system", "content": text}));
        }
    }
    for m in messages {
        let blocks = match m.content {
            Value::String(s) => vec![json!({"type": "text", "text": s})],
            Value::Array(b) => b,
            _ => return Err("message content must be a string or a list of blocks".into()),
        };
        match m.role.as_str() {
            "user" => {
                let mut text = String::new();
                for b in blocks {
                    match b.get("type").and_then(Value::as_str) {
                        Some("text") => {
                            text.push_str(b.get("text").and_then(Value::as_str).unwrap_or_default())
                        }
                        Some("tool_result") => {
                            let content = text_of(b.get("content").unwrap_or(&Value::Null))?;
                            out.push(json!({"role": "tool", "content": content}));
                        }
                        other => return Err(format!("unsupported user content block {other:?}")),
                    }
                }
                if !text.is_empty() {
                    out.push(json!({"role": "user", "content": text}));
                }
            }
            "assistant" => {
                let (mut text, mut thinking, mut calls) =
                    (String::new(), String::new(), Vec::new());
                for b in blocks {
                    match b.get("type").and_then(Value::as_str) {
                        Some("text") => {
                            text.push_str(b.get("text").and_then(Value::as_str).unwrap_or_default())
                        }
                        Some("thinking") => thinking.push_str(
                            b.get("thinking")
                                .and_then(Value::as_str)
                                .unwrap_or_default(),
                        ),
                        Some("redacted_thinking") => {}
                        Some("tool_use") => calls.push(json!({
                            "type": "function",
                            "function": {
                                "name": b.get("name").cloned().unwrap_or_default(),
                                "arguments": b.get("input").cloned().unwrap_or(json!({})),
                            },
                        })),
                        other => {
                            return Err(format!("unsupported assistant content block {other:?}"));
                        }
                    }
                }
                let mut msg = json!({"role": "assistant", "content": text});
                if !thinking.is_empty() {
                    msg["reasoning_content"] = json!(thinking);
                }
                if !calls.is_empty() {
                    msg["tool_calls"] = json!(calls);
                }
                out.push(msg);
            }
            other => return Err(format!("unsupported role {other:?}")),
        }
    }
    Ok(out)
}

pub fn to_gen_request(app: &App, req: MessagesRequest) -> Result<GenRequest, String> {
    let tools = match req
        .tool_choice
        .as_ref()
        .and_then(|c| c.get("type"))
        .and_then(Value::as_str)
    {
        None | Some("auto") => req.tools,
        Some("none") => Vec::new(),
        Some(other) => {
            return Err(format!(
                "tool_choice {other:?} is not supported (use auto or none)"
            ));
        }
    };
    let tools = tools
        .into_iter()
        .map(|t| {
            json!({"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema,
            }})
        })
        .collect();
    let mut sampling = app.defaults.clone();
    if let Some(t) = req.temperature {
        sampling.temperature = t;
    }
    if let Some(p) = req.top_p {
        sampling.top_p = p;
    }
    if let Some(k) = req.top_k {
        sampling.top_k = k;
    }
    crate::validate_sampling(&sampling)?;
    Ok(GenRequest {
        messages: to_messages(req.system.as_ref(), req.messages)?,
        tools,
        template: TemplateOptions {
            reasoning_effort: None,
            enable_thinking: req.thinking.map(|t| t.kind != "disabled"),
        },
        sampling,
        max_tokens: req.max_tokens,
        stop: req.stop_sequences,
    })
}

fn stop_reason(f: Finish) -> &'static str {
    match f {
        Finish::EndTurn => "end_turn",
        Finish::ToolCalls => "tool_use",
        Finish::StopSequence => "stop_sequence",
        Finish::Length => "max_tokens",
    }
}

fn usage_json(u: Usage) -> Value {
    json!({
        "input_tokens": u.prompt_tokens - u.cached_tokens,
        "cache_read_input_tokens": u.cached_tokens,
        "output_tokens": u.completion_tokens,
    })
}

pub async fn messages(State(app): State<Arc<App>>, Json(req): Json<MessagesRequest>) -> Response {
    if !app.is_ready() {
        return error(StatusCode::SERVICE_UNAVAILABLE, "model is loading");
    }
    let stream = req.stream;
    let gen_req = match to_gen_request(&app, req) {
        Ok(r) => r,
        Err(e) => return error(StatusCode::BAD_REQUEST, &e),
    };
    let rx = match generation::start(&app, gen_req) {
        Ok(rx) => rx,
        Err(BadRequest(e)) => return error(StatusCode::BAD_REQUEST, &e),
    };
    let id = app.new_id("msg");
    if stream {
        return stream_response(app, id, rx).into_response();
    }
    match generation::collect(rx).await {
        Ok(c) => {
            let mut content = Vec::new();
            if !c.reasoning.is_empty() {
                content.push(json!({"type": "thinking", "thinking": c.reasoning, "signature": ""}));
            }
            if !c.content.is_empty() {
                content.push(json!({"type": "text", "text": c.content}));
            }
            for t in &c.tool_calls {
                content.push(json!({"type": "tool_use", "id": app.new_id("toolu"), "name": t.name, "input": t.arguments}));
            }
            Json(json!({
                "id": id,
                "type": "message",
                "role": "assistant",
                "model": app.model_name,
                "content": content,
                "stop_reason": c.finish.map(stop_reason),
                "stop_sequence": Value::Null,
                "usage": usage_json(c.usage),
            }))
            .into_response()
        }
        Err(e) => error(StatusCode::INTERNAL_SERVER_ERROR, &e),
    }
}

/// Which content block is open in a stream.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Block {
    Thinking,
    Text,
}

fn stream_response(
    app: Arc<App>,
    id: String,
    mut rx: tokio::sync::mpsc::Receiver<Output>,
) -> Sse<ReceiverStream<Result<Event, Infallible>>> {
    let (tx, out) = tokio::sync::mpsc::channel(64);
    tokio::spawn(async move {
        let ev = |name: &str, data: Value| {
            Ok::<_, Infallible>(Event::default().event(name).data(data.to_string()))
        };
        let start = json!({"type": "message_start", "message": {
            "id": id, "type": "message", "role": "assistant", "model": app.model_name,
            "content": [], "stop_reason": Value::Null, "stop_sequence": Value::Null,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }});
        if tx.send(ev("message_start", start)).await.is_err() {
            return;
        }
        let mut index = 0usize;
        let mut open: Option<Block> = None;
        while let Some(out) = rx.recv().await {
            let mut events = Vec::new();
            let mut open_block = |want: Option<Block>, start: Value, events: &mut Vec<_>| {
                if open.is_some() && open != want {
                    events.push(ev(
                        "content_block_stop",
                        json!({"type": "content_block_stop", "index": index}),
                    ));
                    index += 1;
                    open = None;
                }
                if open.is_none() {
                    events.push(ev(
                        "content_block_start",
                        json!({"type": "content_block_start", "index": index, "content_block": start}),
                    ));
                    open = want;
                }
                index
            };
            match out {
                Output::Piece(Piece::Reasoning(s)) => {
                    let i = open_block(
                        Some(Block::Thinking),
                        json!({"type": "thinking", "thinking": "", "signature": ""}),
                        &mut events,
                    );
                    events.push(ev("content_block_delta", json!({"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": s}})));
                }
                Output::Piece(Piece::Content(s)) => {
                    let i = open_block(
                        Some(Block::Text),
                        json!({"type": "text", "text": ""}),
                        &mut events,
                    );
                    events.push(ev("content_block_delta", json!({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": s}})));
                }
                Output::Piece(Piece::ToolCall(t)) => {
                    // Tool calls arrive whole: open, fill and close a tool_use block.
                    let start = json!({"type": "tool_use", "id": app.new_id("toolu"), "name": t.name, "input": {}});
                    let i = open_block(None, start, &mut events);
                    events.push(ev("content_block_delta", json!({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": t.arguments.to_string()}})));
                    events.push(ev(
                        "content_block_stop",
                        json!({"type": "content_block_stop", "index": i}),
                    ));
                    index += 1;
                    open = None;
                }
                Output::Done(finish, usage) => {
                    if open.is_some() {
                        events.push(ev(
                            "content_block_stop",
                            json!({"type": "content_block_stop", "index": index}),
                        ));
                    }
                    events.push(ev(
                        "message_delta",
                        json!({"type": "message_delta",
                        "delta": {"stop_reason": stop_reason(finish), "stop_sequence": Value::Null},
                        "usage": usage_json(usage)}),
                    ));
                    events.push(ev("message_stop", json!({"type": "message_stop"})));
                }
                Output::Failed(e) => {
                    events.push(ev(
                        "error",
                        json!({"type": "error", "error": {"type": "api_error", "message": e}}),
                    ));
                }
            }
            for e in events {
                if tx.send(e).await.is_err() {
                    return; // client gone; dropping rx cancels generation
                }
            }
        }
    });
    Sse::new(ReceiverStream::new(out))
}
