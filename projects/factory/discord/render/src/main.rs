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

// Largest drawing side the renderers accept before they run: a larger one
// can exhaust the pod's memory before the output check.
const MAX_SIDE: f64 = 4096.0;

fn inline_chart(value: &Value) -> bool {
    match value {
        Value::Object(map) => map.iter().all(|(key, value)| {
            !matches!(key.as_str(), "url" | "href")
                && !(matches!(key.as_str(), "mark" | "type") && value == "image")
                && !(matches!(key.as_str(), "width" | "height")
                    && value.as_f64().is_some_and(|side| side > MAX_SIDE))
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

// An SVG may reference only its own elements: every href and url() points
// at a fragment, and nothing loads images, scripts, style sheets or entities.
fn svg_allowed(input: &str) -> bool {
    let lower = input.to_ascii_lowercase();
    let compact: String = lower.split_whitespace().collect();
    let unsafe_tokens = [
        "<image",
        "<foreignobject",
        "<script",
        "@import",
        "<!entity",
        "<!doctype",
        "xi:include",
        "<feimage",
        "<iframe",
        "<object",
        "<embed",
        "javascript:",
    ];
    if unsafe_tokens.iter().any(|token| compact.contains(token)) {
        return false;
    }
    let fragment_only = |marker: &str| {
        compact.match_indices(marker).all(|(at, _)| {
            let rest = compact[at + marker.len()..].trim_start_matches(['"', '\'', '=']);
            rest.starts_with('#')
        })
    };
    fragment_only("href=") && fragment_only("url(") && svg_size_allowed(&lower)
}

// attribute reads a quoted attribute of one tag: the name must follow
// whitespace, and whitespace may surround the '='.
fn attribute(tag: &str, name: &str) -> Option<String> {
    let mut from = 0;
    while let Some(found) = tag[from..].find(name) {
        let at = from + found;
        from = at + name.len();
        if !tag[..at].ends_with(|c: char| c.is_ascii_whitespace()) {
            continue;
        }
        let rest = tag[from..].trim_start();
        let Some(rest) = rest.strip_prefix('=') else {
            continue;
        };
        let rest = rest.trim_start();
        let quote = rest.chars().next().filter(|c| *c == '"' || *c == '\'')?;
        let value = &rest[1..];
        return Some(value[..value.find(quote)?].trim().to_string());
    }
    None
}

// The root element's width and height, else its viewBox, set the drawing
// size. An explicit side must be a plain or px number no larger than
// MAX_SIDE. A missing or percentage side takes the viewBox's, which must
// also fit; without a viewBox resvg draws 100 px.
fn svg_size_allowed(lower: &str) -> bool {
    let Some(start) = lower.find("<svg") else {
        return false;
    };
    let root = &lower[start..];
    let root = &root[..root.find('>').unwrap_or(root.len())];
    let fits = |n: f64| n > 0.0 && n <= MAX_SIDE;
    let mut from_view_box = false;
    for name in ["width", "height"] {
        match attribute(root, name) {
            Some(v) if v.ends_with('%') => from_view_box = true,
            Some(v) => {
                let number = v.strip_suffix("px").unwrap_or(&v).trim();
                if !number.parse::<f64>().is_ok_and(fits) {
                    return false;
                }
            }
            None => from_view_box = true,
        }
    }
    if !from_view_box {
        return true;
    }
    match attribute(root, "viewbox") {
        None => true,
        Some(v) => {
            let numbers: Vec<f64> = v
                .split(|c: char| c.is_ascii_whitespace() || c == ',')
                .filter(|part| !part.is_empty())
                .filter_map(|part| part.parse().ok())
                .collect();
            numbers.len() == 4 && fits(numbers[2]) && fits(numbers[3])
        }
    }
}

// invariant: HOME-585-2
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
        "/svg" => {
            let spec = std::str::from_utf8(body).map_err(|_| INVALID)?;
            if !svg_allowed(spec) {
                return Err(INVALID);
            }
            process.args(["vl-convert", "svg2png", "-i"]);
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
            (
                "/svg",
                r#"<svg><image href="https://example.org/a.png"/></svg>"#,
            ),
            (
                "/svg",
                r#"<svg><rect fill="url(https://example.org/p)"/></svg>"#,
            ),
            (
                "/svg",
                r#"<svg><a xlink:href = "file:///etc/hostname"><text>x</text></a></svg>"#,
            ),
            ("/svg", r#"<svg><style>@import url(x.css)</style></svg>"#),
            (
                "/svg",
                r#"<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/hostname">]><svg/>"#,
            ),
        ] {
            assert_eq!(
                worker(route, body.as_bytes(), Path::new("in"), Path::new("out")).unwrap_err(),
                INVALID
            );
        }
    }

    #[test]
    fn oversized_drawings_are_refused_before_starting_worker() {
        for (route, body) in [
            ("/svg", r#"<svg width="16000" height="10"/>"#),
            ("/svg", r#"<svg width = '10cm' height="10"/>"#),
            (
                "/svg",
                r#"<svg width="100%" height="100%" viewBox="0 0 20000 10"/>"#,
            ),
            ("/svg", r#"<svg viewBox="0,0,10,9000"/>"#),
            ("/vega-lite", r#"{"width":16000,"mark":"bar"}"#),
            (
                "/vega-lite",
                r#"{"vconcat":[{"height":5000,"mark":"bar"}]}"#,
            ),
        ] {
            assert_eq!(
                worker(route, body.as_bytes(), Path::new("in"), Path::new("out")).unwrap_err(),
                INVALID,
                "{body}"
            );
        }
        for body in [
            r#"<svg width="400px" height="300"/>"#,
            r#"<svg stroke-width="9000" viewBox="0 0 200 200"/>"#,
            r#"<svg/>"#,
        ] {
            assert!(
                worker("/svg", body.as_bytes(), Path::new("in"), Path::new("out")).is_ok(),
                "{body}"
            );
        }
    }

    #[test]
    fn svg_may_reference_its_own_fragments() {
        let svg = r##"<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs><rect fill="url(#g)"/><a href="#g"><text>pelican</text></a></svg>"##;
        assert!(worker("/svg", svg.as_bytes(), Path::new("in"), Path::new("out")).is_ok());
    }

    #[test]
    fn size_limit_applies_to_every_format() {
        for route in ["/vega-lite", "/mermaid", "/svg"] {
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
