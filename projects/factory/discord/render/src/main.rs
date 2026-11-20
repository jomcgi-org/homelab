use serde_json::Value;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use tiny_http::{Header, Method, Request, Response, Server};

const LIMIT: usize = 65536;
type Failure = (u16, &'static str);
const INVALID: Failure = (422, "Unsupported or external input");
const INTERNAL: Failure = (500, "Renderer unavailable");

fn inline_chart(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.iter().all(|(key, value)| {
            !matches!(key.as_str(), "url" | "href")
                && !(matches!(key.as_str(), "mark" | "type") && value == "image")
                && inline_chart(value)
        }),
        Value::Array(items) => items.iter().all(inline_chart),
        _ => true,
    }
}

fn diagram_allowed(input: &str) -> bool {
    let lower = input.to_ascii_lowercase();
    let compact: String = lower.split_whitespace().collect();
    let unsafe_tokens = [
        "%%{", "://", "](", "href", "<img", "<a", "mailto:", "data:", "file:", "@{",
    ];
    !unsafe_tokens.iter().any(|token| compact.contains(token))
        && !input.trim_start().starts_with("---")
        && !lower
            .split(|c: char| !c.is_alphanumeric() && c != '_')
            .any(|word| matches!(word, "click" | "link" | "links" | "callback"))
}

fn worker(route: &str, body: &[u8], input: &Path, output: &Path) -> Result<Command, Failure> {
    if body.len() > LIMIT {
        return Err((413, "Spec exceeds 64 KiB"));
    }
    let mut process = Command::new("timeout");
    process.args(["-s", "KILL", "5"]);
    match route {
        "/vega-lite" => {
            let chart: Value = serde_json::from_slice(body).map_err(|_| INVALID)?;
            if !chart.is_object() || !inline_chart(&chart) {
                return Err(INVALID);
            }
            process.args([
                "vl-convert",
                "vl2png",
                "--allowed-base-url",
                "urn:disabled:",
                "-i",
            ]);
        }
        "/mermaid" => {
            let spec = std::str::from_utf8(body).map_err(|_| INVALID)?;
            if !diagram_allowed(spec) {
                return Err(INVALID);
            }
            process.args(["mmdr", "-e", "png", "-i"]);
        }
        _ => return Err((404, "Unknown renderer")),
    }
    process.arg(input).arg("-o").arg(output);
    Ok(process)
}

fn render(req: &mut Request, dir: &Path) -> Result<File, Failure> {
    if req.method() != &Method::Post {
        return Err((405, "POST required"));
    }
    if req.body_length().is_some_and(|size| size > LIMIT) {
        return Err((413, "Spec exceeds 64 KiB"));
    }
    let mut body = Vec::new();
    req.as_reader()
        .take(LIMIT as u64 + 1)
        .read_to_end(&mut body)
        .map_err(|_| (400, "Incomplete request"))?;
    let input = dir.join("spec");
    let output = dir.join("output.png");
    let mut cmd = worker(req.url(), &body, &input, &output)?;
    fs::write(&input, body).map_err(|_| INTERNAL)?;
    let status = cmd
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|_| INTERNAL)?;
    if !status.success() {
        return Err(match status.code() {
            Some(124 | 137) | None => (504, "Render deadline exceeded"),
            Some(125..=127) => INTERNAL,
            _ => INVALID,
        });
    }
    let mut header = [0u8; 24];
    let mut png = File::open(&output).map_err(|_| INTERNAL)?;
    png.read_exact(&mut header).map_err(|_| INTERNAL)?;
    if !header.starts_with(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR") {
        return Err(INTERNAL);
    }
    for bytes in header[16..].chunks_exact(4) {
        if !(1..=4096).contains(&u32::from_be_bytes(bytes.try_into().unwrap())) {
            return Err((422, "PNG exceeds output bounds"));
        }
    }
    use std::io::Seek;
    png.rewind().map_err(|_| INTERNAL)?;
    Ok(png)
}

fn main() {
    let server = Server::http("0.0.0.0:8080").expect("listen");
    // The private scratch volume and single worker bound simultaneous render cost.
    let dir = std::env::temp_dir().join(format!("render-{}", std::process::id()));
    fs::create_dir_all(&dir).expect("create scratch directory");
    for mut req in server.incoming_requests() {
        let result = render(&mut req, &dir);
        for name in ["spec", "output.png"] {
            let _ = fs::remove_file(dir.join(name));
        }
        match result {
            Ok(png) => {
                let content_type = Header::from_bytes("Content-Type", "image/png").unwrap();
                let _ = req.respond(Response::from_file(png).with_header(content_type));
            }
            Err((code, message)) => {
                let _ = req.respond(Response::from_string(message).with_status_code(code));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_external_resources_before_starting_worker() {
        for (route, body) in [
            (
                "/vega-lite",
                r#"{"layer":[{"data":{"url":"https://example.org/data"}}]}"#,
            ),
            ("/mermaid", "flowchart LR\nA-->B\nclick A \"/report\""),
            ("/mermaid", "sequenceDiagram\nlink Alice: Chart @ /relative"),
        ] {
            assert_eq!(
                worker(route, body.as_bytes(), Path::new("in"), Path::new("out")).unwrap_err(),
                INVALID
            );
        }
    }

    #[test]
    fn size_limit_applies_to_both_formats() {
        for route in ["/vega-lite", "/mermaid"] {
            let result = worker(
                route,
                &vec![b' '; LIMIT + 1],
                Path::new("in"),
                Path::new("out"),
            );
            assert_eq!(result.unwrap_err().0, 413);
        }
    }
}
