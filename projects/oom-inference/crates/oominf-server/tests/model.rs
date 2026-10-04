//! Tests against a converted model directory, ignored by default:
//!
//!     OOMINF_MODEL=/path/to/model.oom cargo test -p oominf-server -- --ignored
//!
//! `real_template_*` only read the tokenizer and chat template; `gpu_smoke` loads
//! the model on the GPU.

use std::path::PathBuf;
use std::sync::Arc;

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode};
use oominf_server::ServeConfig;
use oominf_server::template::{ChatTemplate, TemplateOptions};
use serde_json::{Value, json};
use tower::ServiceExt;

fn model_dir() -> PathBuf {
    std::env::var_os("OOMINF_MODEL")
        .expect("set OOMINF_MODEL to a converted model directory")
        .into()
}

#[test]
#[ignore]
fn real_template_renders_tools_and_tool_history() {
    let t = ChatTemplate::load(&model_dir()).unwrap();
    let tools = [
        json!({"type": "function", "function": {"name": "lookup", "description": "Search",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}),
    ];
    let messages = [
        json!({"role": "user", "content": "find cats"}),
        json!({"role": "assistant", "content": "", "reasoning_content": "search",
               "tool_calls": [{"type": "function", "function": {"name": "lookup", "arguments": {"q": "cats"}}}]}),
        json!({"role": "tool", "content": "3 cats"}),
    ];
    let text = t
        .render(&messages, Some(&tools), &TemplateOptions::default())
        .unwrap();
    assert!(text.contains("# Tools"), "{text}");
    assert!(
        text.contains("<function=lookup>\n<parameter=q>\ncats\n</parameter>"),
        "{text}"
    );
    assert!(
        text.contains("<tool_response>\n3 cats\n</tool_response>"),
        "{text}"
    );
    assert!(text.ends_with("<|im_start|>assistant\n<think>\n"), "{text}");
    let off = t
        .render(
            &messages[..1],
            None,
            &TemplateOptions {
                enable_thinking: Some(false),
                ..Default::default()
            },
        )
        .unwrap();
    assert!(off.ends_with("<think>\n\n</think>\n\n"), "{off}");
    let low = t
        .render(
            &messages[..1],
            None,
            &TemplateOptions {
                reasoning_effort: Some("low".into()),
                ..Default::default()
            },
        )
        .unwrap();
    assert!(low.contains("Reasoning effort is set to low"), "{low}");
}

#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn gpu_smoke() {
    let dir = model_dir();
    let files = Arc::new(oominf_format::Model::open(&dir).unwrap());
    let cfg = ServeConfig {
        model_dir: dir,
        model_type: oominf_models::model_type(&files).unwrap(),
        max_context: 4096,
        draft: 1,
        addr: "127.0.0.1:0".parse().unwrap(),
        model_name: "smoke".into(),
    };
    let loader = Box::new(move || {
        let gpu = Arc::new(oominf_cuda::Gpu::new(0)?);
        let opts = oominf_models::Options {
            max_context: 4096,
            prefill_chunk: None,
        };
        let disk = files.clone();
        oominf_models::open(
            gpu,
            files,
            &opts,
            Box::new(move |_| Ok(Box::new(oominf_tiers::DiskExperts::new(disk)))),
        )
    });
    let (app, loaded) = oominf_server::build(&cfg, loader).unwrap();
    loaded.await.unwrap().unwrap();
    let body = json!({"messages": [{"role": "user", "content": "Say hi."}], "max_tokens": 8, "temperature": 0,
                      "chat_template_kwargs": {"enable_thinking": false}});
    let req = Request::post("/v1/chat/completions")
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let resp = oominf_server::router(Arc::clone(&app))
        .oneshot(req)
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let v: Value =
        serde_json::from_slice(&to_bytes(resp.into_body(), 1 << 20).await.unwrap()).unwrap();
    assert!(v["usage"]["completion_tokens"].as_u64().unwrap() > 0, "{v}");
    assert!(
        !v["choices"][0]["message"]["content"]
            .as_str()
            .unwrap_or_default()
            .is_empty(),
        "{v}"
    );
}
