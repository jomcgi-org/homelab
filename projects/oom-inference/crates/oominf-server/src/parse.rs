//! Splits generated text into reasoning, visible content and tool calls, and
//! applies stop sequences, incrementally so it works for streaming.

use serde_json::{Map, Value};

/// One parsed piece of the model's output.
#[derive(Debug, Clone, PartialEq)]
pub enum Piece {
    Reasoning(String),
    Content(String),
    ToolCall(ToolCall),
}

#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub name: String,
    /// Arguments as a JSON object.
    pub arguments: Value,
}

/// A model family's output format. Implementations are fed decoded text in
/// arbitrary chunks and must hold back anything that could be a partial marker.
pub trait OutputParser: Send {
    fn push(&mut self, text: &str, out: &mut Vec<Piece>);
    /// Flushes whatever is held back at the end of generation.
    fn finish(&mut self, out: &mut Vec<Piece>);
}

/// Tool schemas by function name, used to type parameter values.
pub type ToolSchemas = Map<String, Value>;

/// Builds a parser for one generation from the rendered prompt and the request's
/// tool schemas.
pub type ParserFactory = fn(prompt: &str, schemas: ToolSchemas) -> Box<dyn OutputParser>;

/// The output parser of a model family, by `model_type`.
pub fn parser_for(model_type: &str) -> anyhow::Result<ParserFactory> {
    match model_type {
        "qwen4_exp" => Ok(|prompt, schemas| {
            let in_reasoning = prompt.trim_end_matches('\n').ends_with("<think>");
            Box::new(QwenParser::new(in_reasoning, schemas))
        }),
        other => anyhow::bail!("no output parser for model_type {other:?}"),
    }
}

/// Qwen 3.x: `<think>...</think>` reasoning, then content, with tool calls as
/// `<tool_call><function=NAME><parameter=P>value</parameter>...</function></tool_call>`.
pub struct QwenParser {
    state: State,
    buf: String,
    /// Trailing whitespace held back until we know it is not just before a marker.
    pending_ws: String,
    /// Content has started (leading whitespace after `</think>` is dropped).
    content_started: bool,
    schemas: ToolSchemas,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Reasoning,
    Content,
    ToolCall,
}

const THINK_END: &str = "</think>";
const TOOL_START: &str = "<tool_call>";
const TOOL_END: &str = "</tool_call>";

impl QwenParser {
    /// `in_reasoning` is true when the prompt ended inside an open `<think>` block.
    pub fn new(in_reasoning: bool, schemas: ToolSchemas) -> Self {
        QwenParser {
            state: if in_reasoning {
                State::Reasoning
            } else {
                State::Content
            },
            buf: String::new(),
            pending_ws: String::new(),
            content_started: false,
            schemas,
        }
    }

    fn emit_text(&mut self, text: &str, out: &mut Vec<Piece>) {
        if text.is_empty() {
            return;
        }
        let text = if self.state == State::Content && !self.content_started {
            let t = text.trim_start();
            if t.is_empty() {
                return;
            }
            self.content_started = true;
            t
        } else {
            text
        };
        // Hold back trailing whitespace: it may precede a marker and be dropped.
        let body = text.trim_end();
        let tail = &text[body.len()..];
        if !body.is_empty() {
            let mut s = std::mem::take(&mut self.pending_ws);
            s.push_str(body);
            out.push(match self.state {
                State::Reasoning => Piece::Reasoning(s),
                _ => Piece::Content(s),
            });
        }
        self.pending_ws.push_str(tail);
    }

    fn tool_call(&self, raw: &str) -> Option<ToolCall> {
        let rest = raw.trim().strip_prefix("<function=")?;
        let (name, mut body) = rest.split_once('>')?;
        let name = name.trim().to_owned();
        let props = self
            .schemas
            .get(&name)
            .and_then(|s| s.get("properties"))
            .and_then(Value::as_object);
        let mut args = Map::new();
        while let Some(start) = body.find("<parameter=") {
            let after = &body[start + "<parameter=".len()..];
            let (pname, after) = after.split_once('>')?;
            let end = after.find("</parameter>")?;
            let raw_value = after[..end].strip_prefix('\n').unwrap_or(&after[..end]);
            let raw_value = raw_value.strip_suffix('\n').unwrap_or(raw_value);
            let is_string = props
                .and_then(|p| p.get(pname.trim()))
                .and_then(|p| p.get("type"))
                .and_then(Value::as_str)
                == Some("string");
            let value = if is_string {
                Value::String(raw_value.to_owned())
            } else {
                serde_json::from_str(raw_value)
                    .unwrap_or_else(|_| Value::String(raw_value.to_owned()))
            };
            args.insert(pname.trim().to_owned(), value);
            body = &after[end + "</parameter>".len()..];
        }
        Some(ToolCall {
            name,
            arguments: Value::Object(args),
        })
    }
}

/// Length of the longest suffix of `s` that is a proper prefix of `marker`.
fn partial_marker(s: &str, marker: &str) -> usize {
    (1..marker.len().min(s.len() + 1))
        .rev()
        .find(|&n| s.is_char_boundary(s.len() - n) && marker.starts_with(&s[s.len() - n..]))
        .unwrap_or(0)
}

impl OutputParser for QwenParser {
    fn push(&mut self, text: &str, out: &mut Vec<Piece>) {
        self.buf.push_str(text);
        loop {
            let marker = match self.state {
                State::Reasoning => THINK_END,
                State::Content => TOOL_START,
                State::ToolCall => TOOL_END,
            };
            if let Some(i) = self.buf.find(marker) {
                let before = self.buf[..i].to_owned();
                self.buf.drain(..i + marker.len());
                match self.state {
                    State::Reasoning => {
                        self.emit_text(&before, out);
                        self.pending_ws.clear();
                        self.state = State::Content;
                    }
                    State::Content => {
                        self.emit_text(&before, out);
                        self.pending_ws.clear();
                        self.state = State::ToolCall;
                    }
                    State::ToolCall => {
                        match self.tool_call(&before) {
                            Some(call) => out.push(Piece::ToolCall(call)),
                            // Not a well-formed call: surface it as text.
                            None => {
                                out.push(Piece::Content(format!("{TOOL_START}{before}{TOOL_END}")))
                            }
                        }
                        self.state = State::Content;
                        self.content_started = false;
                    }
                }
                continue;
            }
            if self.state != State::ToolCall {
                let keep = partial_marker(&self.buf, marker);
                let ready: String = self.buf.drain(..self.buf.len() - keep).collect();
                self.emit_text(&ready, out);
            }
            return;
        }
    }

    fn finish(&mut self, out: &mut Vec<Piece>) {
        let rest = std::mem::take(&mut self.buf);
        match self.state {
            // An unterminated tool call is surfaced as text rather than dropped.
            State::ToolCall => out.push(Piece::Content(format!("{TOOL_START}{rest}"))),
            _ => self.emit_text(&rest, out),
        }
        self.pending_ws.clear();
    }
}

/// Watches generated text for caller-supplied stop sequences, holding back any
/// suffix that could still become one.
pub struct StopMatcher {
    stops: Vec<String>,
    held: String,
}

impl StopMatcher {
    pub fn new(stops: Vec<String>) -> Self {
        StopMatcher {
            stops: stops.into_iter().filter(|s| !s.is_empty()).collect(),
            held: String::new(),
        }
    }

    /// Returns the text safe to pass on, and whether a stop sequence was hit (text
    /// after it is discarded).
    pub fn push(&mut self, text: &str) -> (String, bool) {
        self.held.push_str(text);
        if let Some(i) = self
            .stops
            .iter()
            .filter_map(|s| self.held.find(s.as_str()))
            .min()
        {
            let out = self.held[..i].to_owned();
            self.held.clear();
            return (out, true);
        }
        let keep = self
            .stops
            .iter()
            .map(|s| partial_marker(&self.held, s))
            .max()
            .unwrap_or(0);
        let out: String = self.held.drain(..self.held.len() - keep).collect();
        (out, false)
    }

    pub fn finish(&mut self) -> String {
        std::mem::take(&mut self.held)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn run(p: &mut QwenParser, chunks: &[&str]) -> Vec<Piece> {
        let mut out = Vec::new();
        for c in chunks {
            p.push(c, &mut out);
        }
        p.finish(&mut out);
        // Merge consecutive same-kind text pieces for easy comparison.
        let mut merged: Vec<Piece> = Vec::new();
        for piece in out {
            match (merged.last_mut(), piece) {
                (Some(Piece::Reasoning(a)), Piece::Reasoning(b)) => a.push_str(&b),
                (Some(Piece::Content(a)), Piece::Content(b)) => a.push_str(&b),
                (_, p) => merged.push(p),
            }
        }
        merged
    }

    #[test]
    fn splits_reasoning_and_content_across_chunk_boundaries() {
        let text = "Let me think.\nOK.\n</think>\n\nThe answer is 4.";
        for split in 1..text.len() {
            if !text.is_char_boundary(split) {
                continue;
            }
            let mut p = QwenParser::new(true, ToolSchemas::new());
            let got = run(&mut p, &[&text[..split], &text[split..]]);
            assert_eq!(
                got,
                vec![
                    Piece::Reasoning("Let me think.\nOK.".into()),
                    Piece::Content("The answer is 4.".into())
                ],
                "split at {split}"
            );
        }
    }

    #[test]
    fn content_only_when_thinking_disabled() {
        let mut p = QwenParser::new(false, ToolSchemas::new());
        assert_eq!(
            run(&mut p, &["Hello ", "world"]),
            vec![Piece::Content("Hello world".into())]
        );
    }

    #[test]
    fn parses_tool_calls_with_typed_parameters() {
        let mut schemas = ToolSchemas::new();
        schemas.insert(
            "get_weather".into(),
            json!({"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}}}),
        );
        let text = "Need weather.\n</think>\n\nChecking.\n\n<tool_call>\n<function=get_weather>\n<parameter=city>\n42\n</parameter>\n<parameter=days>\n3\n</parameter>\n</function>\n</tool_call>";
        for split in [5, 40, 60, 80, 100, text.len() - 3] {
            let mut p = QwenParser::new(true, schemas.clone());
            let got = run(&mut p, &[&text[..split], &text[split..]]);
            assert_eq!(
                got,
                vec![
                    Piece::Reasoning("Need weather.".into()),
                    Piece::Content("Checking.".into()),
                    Piece::ToolCall(ToolCall {
                        name: "get_weather".into(),
                        arguments: json!({"city": "42", "days": 3})
                    })
                ],
                "split at {split}"
            );
        }
    }

    #[test]
    fn multiple_tool_calls_and_untyped_values() {
        let text = "<tool_call>\n<function=a>\n<parameter=x>\n{\"k\": [1, 2]}\n</parameter>\n</function>\n</tool_call>\n<tool_call>\n<function=b>\n<parameter=y>\nhello world\n</parameter>\n</function>\n</tool_call>";
        let mut p = QwenParser::new(false, ToolSchemas::new());
        let got = run(&mut p, &[text]);
        assert_eq!(
            got,
            vec![
                Piece::ToolCall(ToolCall {
                    name: "a".into(),
                    arguments: json!({"x": {"k": [1, 2]}})
                }),
                Piece::ToolCall(ToolCall {
                    name: "b".into(),
                    arguments: json!({"y": "hello world"})
                }),
            ]
        );
    }

    #[test]
    fn unterminated_tool_call_is_surfaced_as_text() {
        let mut p = QwenParser::new(false, ToolSchemas::new());
        let got = run(&mut p, &["Hi <tool_call>\n<function=a>"]);
        assert_eq!(
            got,
            vec![Piece::Content("Hi<tool_call>\n<function=a>".into())]
        );
    }

    #[test]
    fn stop_sequences_hold_back_partial_matches() {
        let mut s = StopMatcher::new(vec!["STOP".into(), "\n\n".into()]);
        assert_eq!(s.push("abc ST"), ("abc ".into(), false));
        assert_eq!(s.push("OX"), ("STOX".into(), false));
        assert_eq!(s.push("line\n"), ("line".into(), false));
        assert_eq!(s.push("\nrest"), (String::new(), true));
        let mut s = StopMatcher::new(vec![]);
        assert_eq!(s.push("anything"), ("anything".into(), false));
    }

    #[test]
    fn partial_marker_respects_char_boundaries() {
        assert_eq!(partial_marker("héllo </th", "</think>"), 4);
        assert_eq!(partial_marker("é", "</think>"), 0);
        assert_eq!(partial_marker("x<", "<tool_call>"), 1);
    }
}
