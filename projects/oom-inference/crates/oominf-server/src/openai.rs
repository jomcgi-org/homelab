//! OpenAI-compatible `/v1/chat/completions` and `/v1/models`.

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
use crate::parse::{Piece, ToolCall};
use crate::template::TemplateOptions;

#[derive(Debug, Deserialize)]
pub struct ChatRequest {
    pub messages: Vec<Value>,
    #[serde(default)]
    pub tools: Vec<Value>,
    pub tool_choice: Option<Value>,
    #[serde(default)]
    pub stream: bool,
    pub stream_options: Option<StreamOptions>,
    pub max_tokens: Option<usize>,
    pub max_completion_tokens: Option<usize>,
    pub temperature: Option<f32>,
    pub top_p: Option<f32>,
    pub top_k: Option<usize>,
    pub presence_penalty: Option<f32>,
    pub frequency_penalty: Option<f32>,
    pub seed: Option<u64>,
    pub stop: Option<Value>,
    pub n: Option<usize>,
    pub reasoning_effort: Option<String>,
    #[serde(default)]
    pub chat_template_kwargs: TemplateKwargs,
}

#[derive(Debug, Default, Deserialize)]
pub struct StreamOptions {
    #[serde(default)]
    pub include_usage: bool,
}

#[derive(Debug, Default, Deserialize)]
pub struct TemplateKwargs {
    pub enable_thinking: Option<bool>,
    pub reasoning_effort: Option<String>,
}

pub fn error(status: StatusCode, message: &str) -> Response {
    let kind = if status.is_client_error() {
        "invalid_request_error"
    } else {
        "server_error"
    };
    (
        status,
        Json(json!({"error": {"message": message, "type": kind}})),
    )
        .into_response()
}

/// Converts an OpenAI request into the template's message shape.
pub fn to_gen_request(app: &App, req: ChatRequest) -> Result<GenRequest, String> {
    if req.n.is_some_and(|n| n != 1) {
        return Err("only n = 1 is supported".into());
    }
    let tools = match req
        .tool_choice
        .as_ref()
        .map(|c| c.as_str().unwrap_or("specific"))
    {
        None | Some("auto") => req.tools,
        Some("none") => Vec::new(),
        Some(other) => {
            return Err(format!(
                "tool_choice {other:?} is not supported (use auto or none)"
            ));
        }
    };
    let mut messages = req.messages;
    for m in &mut messages {
        // Clients send tool-call arguments as JSON strings; the template needs objects.
        if let Some(calls) = m.get_mut("tool_calls").and_then(Value::as_array_mut) {
            for call in calls {
                if let Some(args) = call.pointer_mut("/function/arguments")
                    && let Some(s) = args.as_str()
                {
                    *args = serde_json::from_str(s)
                        .map_err(|e| format!("tool call arguments are not JSON: {e}"))?;
                }
            }
        }
    }
    let stop = match req.stop {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::String(s)) => vec![s],
        Some(Value::Array(a)) => a
            .into_iter()
            .filter_map(|v| v.as_str().map(str::to_owned))
            .collect(),
        Some(_) => return Err("stop must be a string or an array of strings".into()),
    };
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
    sampling.presence_penalty = req.presence_penalty.unwrap_or(0.0);
    sampling.frequency_penalty = req.frequency_penalty.unwrap_or(0.0);
    sampling.seed = req.seed;
    crate::validate_sampling(&sampling)?;
    Ok(GenRequest {
        messages,
        tools,
        template: TemplateOptions {
            reasoning_effort: req
                .reasoning_effort
                .or(req.chat_template_kwargs.reasoning_effort),
            enable_thinking: req.chat_template_kwargs.enable_thinking,
        },
        sampling,
        max_tokens: req
            .max_completion_tokens
            .or(req.max_tokens)
            .unwrap_or(app.max_context),
        stop,
    })
}

fn finish_reason(f: Finish) -> &'static str {
    match f {
        Finish::EndTurn | Finish::StopSequence => "stop",
        Finish::ToolCalls => "tool_calls",
        Finish::Length => "length",
    }
}

fn usage_json(u: Usage) -> Value {
    json!({
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens": u.prompt_tokens + u.completion_tokens,
        "prompt_tokens_details": {"cached_tokens": u.cached_tokens},
    })
}

fn tool_call_json(app: &App, index: usize, call: &ToolCall) -> Value {
    json!({
        "index": index,
        "id": app.new_id("call"),
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments.to_string()},
    })
}

pub async fn models(State(app): State<Arc<App>>) -> Json<Value> {
    Json(json!({
        "object": "list",
        "data": [{
            "id": app.model_name,
            "object": "model",
            "created": app.started,
            "owned_by": "oominf",
            "context_length": app.max_context,
        }],
    }))
}

pub async fn chat_completions(
    State(app): State<Arc<App>>,
    Json(req): Json<ChatRequest>,
) -> Response {
    if !app.is_ready() {
        return error(StatusCode::SERVICE_UNAVAILABLE, "model is loading");
    }
    let stream = req.stream;
    let include_usage = req.stream_options.as_ref().is_some_and(|o| o.include_usage);
    let gen_req = match to_gen_request(&app, req) {
        Ok(r) => r,
        Err(e) => return error(StatusCode::BAD_REQUEST, &e),
    };
    let rx = match generation::start(&app, gen_req) {
        Ok(rx) => rx,
        Err(BadRequest(e)) => return error(StatusCode::BAD_REQUEST, &e),
    };
    let id = app.new_id("chatcmpl");
    if stream {
        stream_response(app, id, rx, include_usage).into_response()
    } else {
        match generation::collect(rx).await {
            Ok(c) => {
                let calls: Vec<Value> = c
                    .tool_calls
                    .iter()
                    .enumerate()
                    .map(|(i, t)| tool_call_json(&app, i, t))
                    .collect();
                let mut message = json!({
                    "role": "assistant",
                    "content": if c.content.is_empty() { Value::Null } else { Value::String(c.content) },
                });
                if !c.reasoning.is_empty() {
                    message["reasoning_content"] = json!(c.reasoning);
                }
                if !calls.is_empty() {
                    message["tool_calls"] = json!(calls);
                }
                Json(json!({
                    "id": id,
                    "object": "chat.completion",
                    "created": crate::unix_now(),
                    "model": app.model_name,
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": c.finish.map(finish_reason),
                    }],
                    "usage": usage_json(c.usage),
                }))
                .into_response()
            }
            Err(e) => error(StatusCode::INTERNAL_SERVER_ERROR, &e),
        }
    }
}

fn stream_response(
    app: Arc<App>,
    id: String,
    mut rx: tokio::sync::mpsc::Receiver<Output>,
    include_usage: bool,
) -> Sse<ReceiverStream<Result<Event, Infallible>>> {
    let (tx, out) = tokio::sync::mpsc::channel(64);
    tokio::spawn(async move {
        let created = crate::unix_now();
        let chunk = |delta: Value, finish: Option<&str>| {
            json!({
                "id": id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": app.model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })
        };
        let send = |v: Value| Ok::<_, Infallible>(Event::default().data(v.to_string()));
        if tx
            .send(send(chunk(
                json!({"role": "assistant", "content": ""}),
                None,
            )))
            .await
            .is_err()
        {
            return;
        }
        let mut n_calls = 0;
        while let Some(out) = rx.recv().await {
            let event = match out {
                Output::Piece(Piece::Reasoning(s)) => chunk(json!({"reasoning_content": s}), None),
                Output::Piece(Piece::Content(s)) => chunk(json!({"content": s}), None),
                Output::Piece(Piece::ToolCall(t)) => {
                    n_calls += 1;
                    chunk(
                        json!({"tool_calls": [tool_call_json(&app, n_calls - 1, &t)]}),
                        None,
                    )
                }
                Output::Done(finish, usage) => {
                    let _ = tx
                        .send(send(chunk(json!({}), Some(finish_reason(finish)))))
                        .await;
                    if include_usage {
                        let mut u = chunk(json!({}), None);
                        u["choices"] = json!([]);
                        u["usage"] = usage_json(usage);
                        let _ = tx.send(send(u)).await;
                    }
                    break;
                }
                Output::Failed(e) => json!({"error": {"message": e, "type": "server_error"}}),
            };
            if tx.send(send(event)).await.is_err() {
                return; // client gone; dropping rx cancels generation
            }
        }
        let _ = tx.send(Ok(Event::default().data("[DONE]"))).await;
    });
    Sse::new(ReceiverStream::new(out))
}
