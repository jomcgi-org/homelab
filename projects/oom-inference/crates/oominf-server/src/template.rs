//! Chat template rendering and tokenization for a converted model directory.

use std::path::Path;

use anyhow::{Context, Result, anyhow};
use minijinja::{Environment, Error, ErrorKind, Value};
use serde_json::json;

/// Knobs the chat template understands beyond messages and tools.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct TemplateOptions {
    /// `reasoning_effort` (e.g. `xhigh`, `medium`, `low`); `None` keeps the default.
    pub reasoning_effort: Option<String>,
    /// `false` renders an empty thinking block so the model answers directly.
    pub enable_thinking: Option<bool>,
}

pub struct ChatTemplate {
    env: Environment<'static>,
    tokenizer: tokenizers::Tokenizer,
}

fn raise_exception(msg: String) -> std::result::Result<String, Error> {
    Err(Error::new(ErrorKind::InvalidOperation, msg))
}

/// Python's `json.dumps(x, ensure_ascii=False)` layout (`", "` and `": "`
/// separators, insertion order, no HTML escaping), as Hugging Face templates expect.
struct PyJson;

impl serde_json::ser::Formatter for PyJson {
    fn begin_array_value<W: ?Sized + std::io::Write>(
        &mut self,
        w: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
    fn begin_object_key<W: ?Sized + std::io::Write>(
        &mut self,
        w: &mut W,
        first: bool,
    ) -> std::io::Result<()> {
        if first { Ok(()) } else { w.write_all(b", ") }
    }
    fn begin_object_value<W: ?Sized + std::io::Write>(&mut self, w: &mut W) -> std::io::Result<()> {
        w.write_all(b": ")
    }
}

pub fn to_py_json(value: &impl serde::Serialize) -> std::result::Result<String, serde_json::Error> {
    let mut out = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut out, PyJson);
    value.serialize(&mut ser)?;
    Ok(String::from_utf8(out).expect("serde_json writes UTF-8"))
}

fn tojson(value: Value) -> std::result::Result<Value, Error> {
    to_py_json(&value)
        .map(Value::from_safe_string)
        .map_err(|e| Error::new(ErrorKind::InvalidOperation, e.to_string()))
}

impl ChatTemplate {
    pub fn load(model_dir: &Path) -> Result<Self> {
        let tokenizer = tokenizers::Tokenizer::from_file(model_dir.join("tokenizer.json"))
            .map_err(|e| anyhow!("tokenizer.json: {e}"))?;
        let source = std::fs::read_to_string(model_dir.join("chat_template.jinja"))
            .context("chat_template.jinja")?;
        Self::from_parts(tokenizer, source)
    }

    pub fn from_parts(tokenizer: tokenizers::Tokenizer, source: String) -> Result<Self> {
        let mut env = Environment::new();
        minijinja_contrib::add_to_environment(&mut env);
        env.set_unknown_method_callback(minijinja_contrib::pycompat::unknown_method_callback);
        env.add_function("raise_exception", raise_exception);
        env.add_filter("tojson", tojson);
        env.add_template_owned("chat", source)?;
        Ok(ChatTemplate { env, tokenizer })
    }

    /// Renders OpenAI-shaped `messages` (with `tool_calls` arguments as objects) and
    /// optional `tools`, ending with the assistant generation prompt.
    pub fn render(
        &self,
        messages: &[serde_json::Value],
        tools: Option<&[serde_json::Value]>,
        opts: &TemplateOptions,
    ) -> Result<String> {
        let mut ctx = json!({
            "messages": messages,
            "add_generation_prompt": true,
        });
        if let Some(t) = tools.filter(|t| !t.is_empty()) {
            ctx["tools"] = json!(t);
        }
        if let Some(e) = &opts.reasoning_effort {
            ctx["reasoning_effort"] = json!(e);
        }
        if let Some(t) = opts.enable_thinking {
            ctx["enable_thinking"] = json!(t);
        }
        let out = self
            .env
            .get_template("chat")?
            .render(Value::from_serialize(&ctx))?;
        Ok(out)
    }

    /// Prompt positions where a later request is likely to diverge: the starts of
    /// the first and the last user message (a shared system prompt or documents
    /// followed by a different question). `text` and `prompt` are `messages`
    /// rendered and encoded; positions are where the token prefix up to that
    /// message start agrees with `prompt`.
    pub fn reuse_points(
        &self,
        messages: &[serde_json::Value],
        tools: Option<&[serde_json::Value]>,
        opts: &TemplateOptions,
        text: &str,
        prompt: &[u32],
    ) -> Vec<usize> {
        const MARK: &str = "\u{e000}oominf-reuse-point\u{e000}";
        let users: Vec<usize> = messages
            .iter()
            .enumerate()
            .filter(|(_, m)| m["role"] == "user" && m["content"].is_string())
            .map(|(i, _)| i)
            .collect();
        let mut points = Vec::new();
        for i in [users.first(), users.last()].into_iter().flatten().copied() {
            let mut marked = messages.to_vec();
            marked[i]["content"] = serde_json::Value::String(MARK.into());
            let Ok(rendered) = self.render(&marked, tools, opts) else {
                continue;
            };
            let Some(at) = rendered.find(MARK) else {
                continue;
            };
            let head = &rendered[..at];
            if !text.starts_with(head) {
                continue;
            }
            let Ok(ids) = self.encode(head) else {
                continue;
            };
            let shared = ids.iter().zip(prompt).take_while(|(a, b)| a == b).count();
            if shared > 0 {
                points.push(shared);
            }
        }
        points.sort_unstable();
        points.dedup();
        points
    }

    pub fn encode(&self, text: &str) -> Result<Vec<u32>> {
        Ok(self
            .tokenizer
            .encode(text, false)
            .map_err(|e| anyhow!("encode: {e}"))?
            .get_ids()
            .to_vec())
    }

    pub fn decode(&self, ids: &[u32]) -> Result<String> {
        self.tokenizer
            .decode(ids, false)
            .map_err(|e| anyhow!("decode: {e}"))
    }

    pub fn token_id(&self, token: &str) -> Option<u32> {
        self.tokenizer.token_to_id(token)
    }
}

/// Turns a growing list of token ids into text increments without splitting
/// multi-byte characters: text is only emitted once it decodes cleanly.
pub struct Detokenizer {
    ids: Vec<u32>,
    prefix: usize,
    read: usize,
}

impl Default for Detokenizer {
    fn default() -> Self {
        Self::new()
    }
}

impl Detokenizer {
    pub fn new() -> Self {
        Detokenizer {
            ids: Vec::new(),
            prefix: 0,
            read: 0,
        }
    }

    /// Adds one token and returns the newly complete text, if any.
    pub fn push(&mut self, template: &ChatTemplate, id: u32) -> Result<String> {
        self.ids.push(id);
        let before = template.decode(&self.ids[self.prefix..self.read])?;
        let now = template.decode(&self.ids[self.prefix..])?;
        if now.len() > before.len() && !now.ends_with('\u{FFFD}') && now.starts_with(&before) {
            let fresh = now[before.len()..].to_owned();
            self.prefix = self.read;
            self.read = self.ids.len();
            Ok(fresh)
        } else {
            Ok(String::new())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn py_json_matches_python_layout_and_order() {
        let v: serde_json::Value =
            serde_json::from_str(r#"{"name": "f", "a": [1, {"z": "<é>", "b": null}], "t": true}"#)
                .unwrap();
        assert_eq!(
            to_py_json(&v).unwrap(),
            r#"{"name": "f", "a": [1, {"z": "<é>", "b": null}], "t": true}"#
        );
    }
}
