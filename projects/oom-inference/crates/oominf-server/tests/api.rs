//! The HTTP API end to end on the CPU: the real router, template rendering and
//! output parsing, with a scripted stand-in for the engine thread.

use std::sync::Arc;
use std::sync::mpsc as std_mpsc;

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode};
use oominf_server::engine::{EngineHandle, Event, FinishReason, Job};
use oominf_server::sampling::SamplingParams;
use oominf_server::template::ChatTemplate;
use oominf_server::{App, router};
use serde_json::{Value, json};
use tower::ServiceExt;

const WORDS: &[&str] = &[
    "<unk>",
    "<think>",
    "</think>",
    "<|im_end|>",
    "<tool_call>",
    "</tool_call>",
    "\n",
    "hmm",
    "fine",
    "Hello",
    "there",
    "<function=lookup>",
    "<parameter=q>",
    "</parameter>",
    "</function>",
    "cats",
];

/// A word-level tokenizer over `WORDS`, joining tokens without separators.
fn tokenizer() -> tokenizers::Tokenizer {
    let vocab: serde_json::Map<String, Value> = WORDS
        .iter()
        .enumerate()
        .map(|(i, w)| (w.to_string(), json!(i)))
        .collect();
    let added: Vec<Value> = WORDS
        .iter()
        .enumerate()
        .map(|(i, w)| {
            json!({"id": i, "content": w, "single_word": false, "lstrip": false,
            "rstrip": false, "normalized": false, "special": false})
        })
        .collect();
    let spec = json!({
        "version": "1.0", "truncation": null, "padding": null, "added_tokens": added,
        "normalizer": null, "pre_tokenizer": {"type": "Whitespace"}, "post_processor": null,
        "decoder": {"type": "Fuse"},
        "model": {"type": "WordLevel", "vocab": vocab, "unk_token": "<unk>"},
    });
    spec.to_string().parse().unwrap()
}

fn id(word: &str) -> u32 {
    WORDS.iter().position(|w| *w == word).unwrap() as u32
}

const TEMPLATE: &str = "{% for m in messages %}{{ m.role }}: {{ m.content }}\n{% endfor %}\
{% if tools %}tools: {{ tools | length }}\n{% endif %}assistant: <think>\n";

/// An app whose engine replies to every job with `script`, ending with `finish`.
fn app(script: Vec<&'static str>, finish: FinishReason) -> Arc<App> {
    let (tx, rx) = std_mpsc::channel::<Job>();
    std::thread::spawn(move || {
        while let Ok(job) = rx.recv() {
            let _ = job.events.blocking_send(Event::Started {
                prompt_tokens: job.prompt.len(),
                cached: 0,
            });
            for w in &script {
                if job.events.blocking_send(Event::Token(id(w))).is_err() {
                    break;
                }
            }
            let _ = job.events.blocking_send(Event::Finished(finish));
        }
    });
    let template = ChatTemplate::from_parts(tokenizer(), TEMPLATE.into()).unwrap();
    let app = App::new(
        EngineHandle::new(tx),
        template,
        "test-model".into(),
        4096,
        oominf_server::parse::parser_for("qwen4_exp").unwrap(),
        SamplingParams::default(),
        vec![id("<|im_end|>")],
    );
    app.set_ready();
    Arc::new(app)
}

async fn post(app: Arc<App>, path: &str, body: Value) -> (StatusCode, String) {
    let req = Request::post(path)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = router(app).oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), 1 << 20).await.unwrap();
    (status, String::from_utf8(bytes.to_vec()).unwrap())
}

/// `data:` payloads of an SSE body, with the event name when present.
fn sse(body: &str) -> Vec<(Option<String>, String)> {
    body.split("\n\n")
        .filter(|f| !f.trim().is_empty())
        .map(|frame| {
            let mut event = None;
            let mut data = String::new();
            for line in frame.lines() {
                if let Some(e) = line.strip_prefix("event: ") {
                    event = Some(e.to_owned());
                } else if let Some(d) = line.strip_prefix("data: ") {
                    data.push_str(d);
                }
            }
            (event, data)
        })
        .collect()
}

const ANSWER: &[&str] = &["hmm", "\n", "</think>", "\n", "Hello", "there"];

#[tokio::test]
async fn openai_non_streaming_splits_reasoning_and_counts_usage() {
    let (status, body) = post(
        app(ANSWER.to_vec(), FinishReason::Stop),
        "/v1/chat/completions",
        json!({"model": "x", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let v: Value = serde_json::from_str(&body).unwrap();
    let msg = &v["choices"][0]["message"];
    assert_eq!(msg["reasoning_content"], "hmm");
    assert_eq!(msg["content"], "Hellothere");
    assert_eq!(v["choices"][0]["finish_reason"], "stop");
    assert_eq!(v["usage"]["completion_tokens"], ANSWER.len());
    assert_eq!(v["model"], "test-model");
}

#[tokio::test]
async fn openai_streaming_frames_deltas_and_ends_with_done() {
    let (status, body) = post(
        app(ANSWER.to_vec(), FinishReason::Stop),
        "/v1/chat/completions",
        json!({"messages": [{"role": "user", "content": "hi"}], "stream": true,
               "stream_options": {"include_usage": true}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let frames = sse(&body);
    assert_eq!(frames.last().unwrap().1, "[DONE]");
    let chunks: Vec<Value> = frames[..frames.len() - 1]
        .iter()
        .map(|(_, d)| serde_json::from_str(d).unwrap())
        .collect();
    assert_eq!(chunks[0]["choices"][0]["delta"]["role"], "assistant");
    let text = |k: &str| -> String {
        chunks
            .iter()
            .filter_map(|c| c["choices"][0]["delta"][k].as_str())
            .collect()
    };
    assert_eq!(text("reasoning_content"), "hmm");
    assert_eq!(text("content"), "Hellothere");
    let finish: Vec<&Value> = chunks
        .iter()
        .map(|c| &c["choices"][0]["finish_reason"])
        .filter(|f| !f.is_null())
        .collect();
    assert_eq!(finish, vec![&json!("stop")]);
    assert_eq!(
        chunks.last().unwrap()["usage"]["completion_tokens"],
        ANSWER.len()
    );
}

#[tokio::test]
async fn openai_tool_calls_are_parsed_with_string_arguments() {
    let script = vec![
        "fine",
        "</think>",
        "<tool_call>",
        "\n",
        "<function=lookup>",
        "\n",
        "<parameter=q>",
        "\n",
        "cats",
        "\n",
        "</parameter>",
        "\n",
        "</function>",
        "\n",
        "</tool_call>",
    ];
    let tools = json!([{"type": "function", "function": {"name": "lookup",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}]);
    let (status, body) = post(
        app(script, FinishReason::Stop),
        "/v1/chat/completions",
        json!({"messages": [{"role": "user", "content": "find"}], "tools": tools}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let v: Value = serde_json::from_str(&body).unwrap();
    let call = &v["choices"][0]["message"]["tool_calls"][0];
    assert_eq!(call["function"]["name"], "lookup");
    assert_eq!(
        serde_json::from_str::<Value>(call["function"]["arguments"].as_str().unwrap()).unwrap(),
        json!({"q": "cats"})
    );
    assert_eq!(v["choices"][0]["finish_reason"], "tool_calls");
    assert!(v["choices"][0]["message"]["content"].is_null());
}

#[tokio::test]
async fn stop_sequences_and_length_finish() {
    let (_, body) = post(
        app(ANSWER.to_vec(), FinishReason::Length),
        "/v1/chat/completions",
        json!({"messages": [{"role": "user", "content": "hi"}], "stop": "there"}),
    )
    .await;
    let v: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["choices"][0]["message"]["content"], "Hello");
    assert_eq!(v["choices"][0]["finish_reason"], "stop");

    let (_, body) = post(
        app(ANSWER.to_vec(), FinishReason::Length),
        "/v1/chat/completions",
        json!({"messages": [{"role": "user", "content": "hi"}]}),
    )
    .await;
    let v: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["choices"][0]["finish_reason"], "length");
}

#[tokio::test]
async fn rejects_invalid_requests() {
    let a = app(ANSWER.to_vec(), FinishReason::Stop);
    for (path, body) in [
        (
            "/v1/chat/completions",
            json!({"messages": [{"role": "user", "content": "hi"}], "n": 2}),
        ),
        (
            "/v1/chat/completions",
            json!({"messages": [{"role": "user", "content": "hi"}], "temperature": 5}),
        ),
        (
            "/v1/chat/completions",
            json!({"messages": [{"role": "user", "content": "hi"}], "tool_choice": "required"}),
        ),
        (
            "/v1/messages",
            json!({"max_tokens": 5, "messages": [{"role": "user", "content": [{"type": "image"}]}]}),
        ),
    ] {
        let (status, body) = post(a.clone(), path, body).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{path}: {body}");
    }
}

#[tokio::test]
async fn anthropic_non_streaming_returns_thinking_text_and_usage() {
    let (status, body) = post(
        app(ANSWER.to_vec(), FinishReason::Stop),
        "/v1/messages",
        json!({"max_tokens": 64, "system": "be brief", "messages": [{"role": "user", "content": "hi"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let v: Value = serde_json::from_str(&body).unwrap();
    assert_eq!(v["content"][0]["type"], "thinking");
    assert_eq!(v["content"][0]["thinking"], "hmm");
    assert_eq!(
        v["content"][1],
        json!({"type": "text", "text": "Hellothere"})
    );
    assert_eq!(v["stop_reason"], "end_turn");
    assert_eq!(v["usage"]["output_tokens"], ANSWER.len());
}

#[tokio::test]
async fn anthropic_streaming_emits_ordered_blocks_and_tool_use() {
    let script = vec![
        "hmm",
        "</think>",
        "Hello",
        "<tool_call>",
        "<function=lookup>",
        "<parameter=q>",
        "\n",
        "cats",
        "\n",
        "</parameter>",
        "</function>",
        "</tool_call>",
    ];
    let (status, body) = post(
        app(script, FinishReason::Stop),
        "/v1/messages",
        json!({"max_tokens": 64, "stream": true, "messages": [{"role": "user", "content": "hi"}],
               "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let frames = sse(&body);
    let names: Vec<String> = frames.iter().map(|(e, _)| e.clone().unwrap()).collect();
    assert_eq!(names.first().unwrap(), "message_start");
    assert_eq!(names.last().unwrap(), "message_stop");
    let starts: Vec<Value> = frames
        .iter()
        .filter(|(e, _)| e.as_deref() == Some("content_block_start"))
        .map(|(_, d)| serde_json::from_str::<Value>(d).unwrap())
        .collect();
    let kinds: Vec<&str> = starts
        .iter()
        .map(|s| s["content_block"]["type"].as_str().unwrap())
        .collect();
    assert_eq!(kinds, ["thinking", "text", "tool_use"]);
    let indexes: Vec<u64> = starts
        .iter()
        .map(|s| s["index"].as_u64().unwrap())
        .collect();
    assert_eq!(indexes, [0, 1, 2]);
    let delta: Value = frames
        .iter()
        .filter(|(e, _)| e.as_deref() == Some("message_delta"))
        .map(|(_, d)| serde_json::from_str(d).unwrap())
        .next()
        .unwrap();
    assert_eq!(delta["delta"]["stop_reason"], "tool_use");
    let json_delta = frames
        .iter()
        .find(|(_, d)| d.contains("input_json_delta"))
        .map(|(_, d)| serde_json::from_str::<Value>(d).unwrap())
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(json_delta["delta"]["partial_json"].as_str().unwrap())
            .unwrap(),
        json!({"q": "cats"})
    );
}

#[tokio::test]
async fn anthropic_history_maps_to_template_messages() {
    let messages = vec![
        oominf_server::anthropic::Message {
            role: "user".into(),
            content: json!("find cats"),
        },
        oominf_server::anthropic::Message {
            role: "assistant".into(),
            content: json!([
                {"type": "thinking", "thinking": "look up"},
                {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": "cats"}}
            ]),
        },
        oominf_server::anthropic::Message {
            role: "user".into(),
            content: json!([{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "3 cats"}]}]),
        },
    ];
    let out = oominf_server::anthropic::to_messages(Some(&json!("sys")), messages).unwrap();
    assert_eq!(
        Value::Array(out),
        json!([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "find cats"},
            {"role": "assistant", "content": "", "reasoning_content": "look up",
             "tool_calls": [{"type": "function", "function": {"name": "lookup", "arguments": {"q": "cats"}}}]},
            {"role": "tool", "content": "3 cats"}
        ])
    );
}

/// With the waiting queue full, both APIs refuse at once with 429 and
/// `Retry-After`, and the refusal is counted; the queued request is unaffected.
#[tokio::test]
async fn full_queue_returns_429_with_retry_after() {
    // An engine that takes jobs and never serves them: each stays waiting.
    let (tx, rx) = std_mpsc::channel::<Job>();
    std::thread::spawn(move || {
        let mut held = Vec::new();
        while let Ok(job) = rx.recv() {
            held.push(job);
        }
    });
    let template = ChatTemplate::from_parts(tokenizer(), TEMPLATE.into()).unwrap();
    let app = Arc::new(App::new(
        EngineHandle::bounded(tx, 1),
        template,
        "m".into(),
        4096,
        oominf_server::parse::parser_for("qwen4_exp").unwrap(),
        SamplingParams::default(),
        vec![id("<|im_end|>")],
    ));
    app.set_ready();
    let first = tokio::spawn(post(
        app.clone(),
        "/v1/chat/completions",
        json!({"messages": [{"role": "user", "content": "hi"}]}),
    ));
    while app.engine.queue().0 == 0 {
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
    for (path, body) in [
        (
            "/v1/chat/completions",
            json!({"messages": [{"role": "user", "content": "hi"}]}),
        ),
        (
            "/v1/messages",
            json!({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 4}),
        ),
    ] {
        let req = Request::post(path)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let resp = router(app.clone()).oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS, "{path}");
        assert_eq!(resp.headers()["retry-after"], "5", "{path}");
        let body: Value =
            serde_json::from_slice(&to_bytes(resp.into_body(), 1 << 20).await.unwrap()).unwrap();
        // Both flavours name it `rate_limit_error` under `error.type`.
        assert_eq!(body["error"]["type"], "rate_limit_error", "{path}: {body}");
    }
    assert_eq!(app.engine.queue(), (1, 2));
    assert!(!first.is_finished());
    first.abort();
}

#[tokio::test]
async fn not_ready_returns_503() {
    let (tx, _rx) = std_mpsc::channel::<Job>();
    let template = ChatTemplate::from_parts(tokenizer(), TEMPLATE.into()).unwrap();
    let a = Arc::new(App::new(
        EngineHandle::new(tx),
        template,
        "m".into(),
        64,
        oominf_server::parse::parser_for("qwen4_exp").unwrap(),
        SamplingParams::default(),
        vec![0],
    ));
    let (status, _) = post(
        a,
        "/v1/chat/completions",
        json!({"messages": [{"role": "user", "content": "hi"}]}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
}
