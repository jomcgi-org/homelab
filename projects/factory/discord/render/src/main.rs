mod table;

use serde_json::Value;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use tiny_http::{Header, Method, Request, Response, Server};

const LIMIT: usize = 65536;
type Failure = (u16, &'static str);
const INVALID: Failure = (422, "Unsupported or external input");
const INTERNAL: Failure = (500, "Renderer unavailable");

// Largest drawing side the renderers accept before they run: a larger one
// can exhaust the pod's memory before the output check.
const MAX_SIDE: f64 = 4096.0;

// The house theme. The renderer applies it to every diagram and chart, so
// the agent cannot pick a different font, palette or background. Mermaid
// source cannot carry its own theme: diagram_allowed refuses "%%{" and
// front matter.
const MERMAID_THEME: &str = include_str!("../theme/mermaid.json");
const VEGA_LITE_THEME: &str = include_str!("../theme/vega-lite.json");

// Discord fits an image into a box about 550 CSS pixels wide on desktop
// and about 390 points wide on a phone with 3 device pixels to a point. A
// PNG about 2048 pixels wide stays sharp in both and in the full-size view.
// A drawing rasterizes at TARGET_WIDTH / width, at least SCALE and at most
// MAX_SCALE, within MAX_SIDE.
const TARGET_WIDTH: f64 = 2048.0;
const SCALE: f64 = 2.0;
const MAX_SCALE: f64 = 8.0;

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
// size. This catches a model asking for a huge canvas; it is not a bound on
// hostile input (a comment or pattern tile gets past it), which needs a
// memory limit per worker. A side is a plain or px number, a percentage of
// the viewBox's side, or em/ex at 16 px each; it must be no larger than
// MAX_SIDE. A missing side takes the viewBox's, which must also fit.
// Without a viewBox the check counts 100 px, but resvg sizes such an image
// to its content; the PNG check after the raster bounds it.
fn svg_size_allowed(lower: &str) -> bool {
    svg_size(lower).is_some()
}

// The drawing's size in pixels.
#[derive(Debug, PartialEq)]
struct Drawing {
    width: f64,
    height: f64,
}

// svg_size returns the drawing's size from the lowercased SVG, or None when
// a side does not fit. It reads a missing or percentage side as resvg does.
fn svg_size(lower: &str) -> Option<Drawing> {
    let start = lower.find("<svg")?;
    let root = &lower[start..];
    let root = &root[..root.find('>').unwrap_or(root.len())];
    let fits = |n: f64| n > 0.0 && n <= MAX_SIDE;
    // The viewBox counts only when a side takes its size from it.
    let view_box = attribute(root, "viewbox").map(|v| {
        let numbers: Vec<f64> = v
            .split(|c: char| c.is_ascii_whitespace() || c == ',')
            .filter(|part| !part.is_empty())
            .filter_map(|part| part.parse().ok())
            .collect();
        (numbers.len() == 4 && fits(numbers[2]) && fits(numbers[3]))
            .then(|| (numbers[2], numbers[3]))
    });
    let mut sides = [0.0; 2];
    for (index, name) in ["width", "height"].into_iter().enumerate() {
        // resvg takes a missing or percentage side from the viewBox's side
        // on the same axis, or 100 px without a viewBox.
        let view_side = || match view_box {
            None => Some(100.0),
            Some(Some((w, h))) => Some(if index == 0 { w } else { h }),
            Some(None) => None,
        };
        sides[index] = match attribute(root, name) {
            Some(v) if v.ends_with('%') => {
                let n = v[..v.len() - 1].trim().parse::<f64>().ok()?;
                view_side()? * n / 100.0
            }
            // An em or ex side is that many font sizes; count 16 px each.
            // A root font-size would change that size, so it is refused.
            Some(v) if v.ends_with("em") || v.ends_with("ex") => {
                if root.contains("font-size") {
                    return None;
                }
                v[..v.len() - 2].trim().parse::<f64>().ok()? * 16.0
            }
            Some(v) => {
                let number = v.strip_suffix("px").unwrap_or(&v).trim();
                number.parse::<f64>().ok()?
            }
            None => view_side()?,
        };
        if !fits(sides[index]) {
            return None;
        }
    }
    Some(Drawing {
        width: sides[0],
        height: sides[1],
    })
}

// exact_size reads the SVG as resvg does: case-sensitive names, and only
// plain, px or percentage lengths with no spaces. It returns the size when
// resvg takes it from the root, and None when resvg sizes the image to its
// content (no viewBox and not both sides) or the root is not that simple.
// The root must come first, after at most an XML declaration, so a comment
// cannot hold a decoy root.
fn exact_size(svg: &str) -> Option<Drawing> {
    let start = svg.find("<svg")?;
    let prefix = svg[..start].trim();
    let declaration =
        prefix.starts_with("<?xml") && prefix.ends_with("?>") && prefix.matches('<').count() == 1;
    if !prefix.is_empty() && !declaration {
        return None;
    }
    let attributes = root_attributes(&svg[start + 4..])?;
    let get = |name: &str| {
        attributes
            .iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| *value)
    };
    let number = |v: &str| {
        v.parse::<f64>()
            .ok()
            .filter(|n| n.is_finite() && *n > 0.0 && !v.contains(char::is_whitespace))
    };
    let view_box = match get("viewBox") {
        None => None,
        Some(v) => {
            let parts: Vec<&str> = v
                .split(|c: char| c.is_ascii_whitespace() || c == ',')
                .filter(|part| !part.is_empty())
                .collect();
            let origin = |p: &&str| p.parse::<f64>().is_ok_and(f64::is_finite);
            if parts.len() != 4 || !parts[..2].iter().all(origin) {
                return None;
            }
            Some((number(parts[2])?, number(parts[3])?))
        }
    };
    let mut sides = [0.0; 2];
    for (index, name) in ["width", "height"].into_iter().enumerate() {
        let view_side = view_box.map(|(w, h)| if index == 0 { w } else { h });
        sides[index] = match get(name) {
            None => view_side?,
            Some(v) => match v.strip_suffix('%') {
                Some(percent) => view_side? * number(percent)? / 100.0,
                None => number(v.strip_suffix("px").unwrap_or(v))?,
            },
        };
    }
    Some(Drawing {
        width: sides[0],
        height: sides[1],
    })
}

// root_attributes reads the root tag's attributes one by one, so a name
// inside another attribute's value is not read as an attribute. It returns
// None for anything but name="value" pairs up to the tag's end, or for a
// name given twice.
fn root_attributes(mut rest: &str) -> Option<Vec<(&str, &str)>> {
    let mut attributes: Vec<(&str, &str)> = Vec::new();
    loop {
        let trimmed = rest.trim_start();
        if trimmed.starts_with('>') || trimmed.starts_with("/>") {
            return Some(attributes);
        }
        if trimmed.len() == rest.len() {
            return None;
        }
        let end = trimmed
            .find(|c: char| !(c.is_ascii_alphanumeric() || matches!(c, ':' | '_' | '-' | '.')))?;
        let (name, after) = trimmed.split_at(end);
        let after = after.trim_start().strip_prefix('=')?.trim_start();
        let quote = after.chars().next().filter(|c| *c == '"' || *c == '\'')?;
        let value_end = after[1..].find(quote)?;
        if name.is_empty() || attributes.iter().any(|(key, _)| *key == name) {
            return None;
        }
        attributes.push((name, &after[1..1 + value_end]));
        rest = &after[2 + value_end..];
    }
}

// raster_scale picks the PNG's scale: TARGET_WIDTH pixels wide, at least
// SCALE and at most MAX_SCALE, with neither side over MAX_SIDE. A drawing
// whose size is not exact keeps SCALE, the old rule.
fn raster_scale(drawing: &Drawing, exact: bool) -> f64 {
    let wanted = if exact {
        (TARGET_WIDTH / drawing.width).clamp(SCALE, MAX_SCALE)
    } else {
        SCALE
    };
    let fit = MAX_SIDE / drawing.width.max(drawing.height);
    // Round down so that a side times the scale stays within MAX_SIDE.
    (wanted.min(fit).max(1.0) * 1000.0).floor() / 1000.0
}

// merge_theme writes each house value over the chart's config, so a chart
// keeps its own settings for everything the house theme does not set.
fn merge_theme(target: &mut Value, house: Value) {
    match (target, house) {
        (Value::Object(target), Value::Object(house)) => {
            for (key, value) in house {
                merge_theme(target.entry(key).or_insert(Value::Null), value);
            }
        }
        (target, house) => *target = house,
    }
}

// A request becomes an SVG drawing, then a PNG. Job.input is the file the
// renderer writes; Job.command, when present, turns it into the SVG.
#[derive(Debug)]
struct Job {
    input: Vec<u8>,
    command: Option<Command>,
}

fn deadline(seconds: &str) -> Command {
    let mut process = Command::new("timeout");
    process.args(["-s", "KILL", seconds]);
    process
}

// invariant: HOME-585-2
fn worker(route: &str, body: &[u8], input: &Path, svg: &Path) -> Result<Job, Failure> {
    if body.len() > LIMIT {
        return Err((413, "Spec exceeds 64 KiB"));
    }
    // The drawing step and the raster step share the five-second deadline.
    let mut process = deadline("3");
    let spec = match route {
        "/vega-lite" => {
            let mut chart: Value = serde_json::from_slice(body).map_err(|_| INVALID)?;
            if !chart.is_object() || !inline_chart(&chart) {
                return Err(INVALID);
            }
            let house = serde_json::from_str(VEGA_LITE_THEME).map_err(|_| INTERNAL)?;
            let map = chart.as_object_mut().ok_or(INVALID)?;
            map.remove("background");
            merge_theme(map.entry("config").or_insert(Value::Null), house);
            process.args([
                "vl-convert",
                "vl2svg",
                "--allowed-base-url",
                "urn:disabled:",
                "-i",
            ]);
            serde_json::to_vec(&chart).map_err(|_| INTERNAL)?
        }
        "/mermaid" => {
            let spec = std::str::from_utf8(body).map_err(|_| INVALID)?;
            if !diagram_allowed(spec) {
                return Err(INVALID);
            }
            process
                .arg("mmdr")
                .arg("-c")
                .arg(input.with_file_name("mermaid.json"));
            process.args(["-e", "svg", "-i"]);
            body.to_vec()
        }
        "/svg" => {
            let spec = std::str::from_utf8(body).map_err(|_| INVALID)?;
            if !svg_allowed(spec) {
                return Err(INVALID);
            }
            return Ok(Job {
                input: body.to_vec(),
                command: None,
            });
        }
        "/table" => {
            let drawing = table::svg(body).ok_or(INVALID)?;
            return Ok(Job {
                input: drawing.into_bytes(),
                command: None,
            });
        }
        _ => return Err((404, "Unknown renderer")),
    };
    process.arg(input).arg("-o").arg(svg);
    Ok(Job {
        input: spec,
        command: Some(process),
    })
}

fn run(mut process: Command) -> Result<(), Failure> {
    let status = process
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|_| INTERNAL)?;
    if status.success() {
        return Ok(());
    }
    Err(match status.code() {
        Some(124 | 137) | None => (504, "Render deadline exceeded"),
        Some(125..=127) => INTERNAL,
        _ => INVALID,
    })
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
    let drawing = dir.join("drawing.svg");
    let output = dir.join("output.png");
    let job = worker(req.url(), &body, &input, &drawing)?;
    fs::write(&input, &job.input).map_err(|_| INTERNAL)?;
    // An agent's SVG and a table have no drawing step, so their raster
    // gets the whole deadline.
    let (drawing, raster_seconds): (PathBuf, &str) = match job.command {
        Some(process) => {
            run(process)?;
            (drawing, "2")
        }
        None => (input, "5"),
    };
    // The root tag starts the converters' drawings; svg_allowed has read
    // the whole of an agent's SVG, so its first 64 KiB hold the root.
    let mut head = Vec::new();
    File::open(&drawing)
        .and_then(|file| file.take(LIMIT as u64).read_to_end(&mut head))
        .map_err(|_| INTERNAL)?;
    let head = String::from_utf8_lossy(&head);
    let drawing_size =
        svg_size(&head.to_ascii_lowercase()).ok_or((422, "PNG exceeds output bounds"))?;
    // The target scale needs the size resvg will use; any doubt keeps SCALE.
    let exact = exact_size(&head).as_ref() == Some(&drawing_size);
    let scale = format!("{:.3}", raster_scale(&drawing_size, exact));
    let mut raster = deadline(raster_seconds);
    raster
        .args(["vl-convert", "svg2png", "--scale", &scale, "-i"])
        .arg(&drawing)
        .arg("-o")
        .arg(&output);
    run(raster)?;
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
    fs::write(dir.join("mermaid.json"), MERMAID_THEME).expect("write Mermaid theme");
    for mut req in server.incoming_requests() {
        let result = render(&mut req, &dir);
        for name in ["spec", "drawing.svg", "output.png"] {
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
            r#"<svg width="1em" height="1em" viewBox="0 0 24 24"/>"#,
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
    fn house_theme_wins_over_the_chart_config() {
        let chart = r##"{"background":"#000","mark":"bar","config":{"font":"Comic Sans MS","axis":{"labelAngle":45,"labelColor":"red"}}}"##;
        let job = worker(
            "/vega-lite",
            chart.as_bytes(),
            Path::new("in"),
            Path::new("out"),
        )
        .unwrap();
        let themed: Value = serde_json::from_slice(&job.input).unwrap();
        assert_eq!(themed.get("background"), None);
        assert_eq!(themed["config"]["font"], "Inter");
        assert_eq!(themed["config"]["background"], "#FFFFFF");
        assert_eq!(themed["config"]["axis"]["labelAngle"], 45);
        assert_eq!(themed["config"]["axis"]["labelColor"], "#475569");
        let mermaid: Value = serde_json::from_str(MERMAID_THEME).unwrap();
        assert_eq!(mermaid["theme"], "modern");
    }

    fn size(width: f64, height: f64) -> Option<Drawing> {
        Some(Drawing { width, height })
    }

    #[test]
    fn drawing_size_follows_resvg() {
        assert_eq!(
            svg_size(r#"<svg width="300" height="1500.5">"#),
            size(300.0, 1500.5)
        );
        assert_eq!(
            svg_size(r#"<svg viewbox="0 0 24 24" width="1em">"#),
            size(16.0, 24.0)
        );
        assert_eq!(svg_size("<svg>"), size(100.0, 100.0));
        assert_eq!(svg_size(r#"<svg width="5000" height="10">"#), None);
        assert_eq!(
            svg_size(r#"<svg width="200%" height="200%" viewbox="0 0 2048 2048">"#),
            size(4096.0, 4096.0)
        );
        assert_eq!(
            svg_size(r#"<svg width="300%" viewbox="0 0 2048 10">"#),
            None
        );
        // vl-convert 1.9.0 draws these at 100x40 and 200x200 pixels.
        assert_eq!(
            svg_size(r#"<svg width="100" viewbox="0 0 1 40">"#),
            size(100.0, 40.0)
        );
        assert_eq!(
            svg_size(r#"<svg width="50%" viewbox="0 0 400 200">"#),
            size(200.0, 200.0)
        );
        // Two absolute sides ignore the viewBox, as before.
        assert_eq!(
            svg_size(r#"<svg width="400" height="300" viewbox="0 0 20000 10">"#),
            size(400.0, 300.0)
        );
        assert_eq!(
            svg_size(r#"<svg width="200em" viewbox="0 0 24 24">"#),
            size(3200.0, 24.0)
        );
        assert_eq!(svg_size(r#"<svg width="300em">"#), None);
        assert_eq!(svg_size(r#"<svg font-size="4096" width="4em">"#), None);
    }

    #[test]
    fn exact_size_only_trusts_what_resvg_reads() {
        // The converters' roots.
        assert_eq!(
            exact_size(r#"<svg width="1793.3381" height="78" viewBox="0 0 1793.3381 78">"#),
            size(1793.3381, 78.0)
        );
        assert_eq!(
            exact_size(r#"<svg width="100%" viewBox="0,0,400,200">"#),
            size(400.0, 200.0)
        );
        assert_eq!(
            exact_size(r#"<svg width="300px" height="200">"#),
            size(300.0, 200.0)
        );
        assert_eq!(
            exact_size(r#"<?xml version="1.0"?> <svg width="300" height="200"/>"#),
            size(300.0, 200.0)
        );
        // resvg sizes these to their content, or reads them differently
        // from the lowercased check.
        for root in [
            "<svg>",
            r#"<svg width="300">"#,
            r#"<svg WIDTH="100" HEIGHT="100">"#,
            r#"<svg width="100PX" height="100">"#,
            r#"<svg width="100 " height="100">"#,
            r#"<svg width="1em" height="1em">"#,
            r#"<svg viewbox="0 0 100 100">"#,
            r#"<svg viewBox="x 0 0 100 100">"#,
            r#"<svg viewBox="0 0 0x10 100 100">"#,
            r#"<svg viewBox="0 0 inf 100">"#,
            r#"<SVG width="10" height="10">"#,
            r#"<svg viewBox="inf 0 100 100">"#,
            r#"<svg viewBox="NaN 0 100 100">"#,
            // A decoy root in a comment, or a decoy size inside a value.
            r#"<!-- <svg width="10" height="10"> --><svg viewBox="0 0 2000 2000">"#,
            r#"<svg data-x=" width='10' height='10' ">"#,
            r#"<svg width="10" width="20" height="10">"#,
            r#"<svg width="10"height="10">"#,
        ] {
            assert_eq!(exact_size(root), None, "{root}");
        }
    }

    #[test]
    fn raster_scale_targets_a_sharp_width_within_bounds() {
        let scale = |width, height, exact| raster_scale(&Drawing { width, height }, exact);
        // A small chart stops at MAX_SCALE.
        assert_eq!(scale(184.0, 270.0, true), MAX_SCALE);
        assert_eq!(scale(512.0, 300.0, true), 4.0);
        assert_eq!(scale(1024.0, 300.0, true), 2.0);
        // A wide drawing keeps at least SCALE while it fits.
        assert_eq!(scale(1793.3, 78.0, true), 2.0);
        // A tall or large drawing stops at MAX_SIDE.
        assert_eq!(scale(221.0, 830.0, true), 4.934);
        assert_eq!(scale(3000.0, 200.0, true), 1.365);
        assert_eq!(scale(4096.0, 4096.0, true), 1.0);
        // A drawing whose size is not exact keeps the old 2x rule.
        assert_eq!(scale(100.0, 100.0, false), 2.0);
        assert_eq!(scale(24.0, 24.0, true), MAX_SCALE);
        for (width, height) in [
            (184.0, 270.0),
            (221.0, 830.0),
            (3000.0, 200.0),
            (2047.9, 13.0),
        ] {
            let s = scale(width, height, true);
            assert!(width * s <= MAX_SIDE && height * s <= MAX_SIDE);
        }
    }

    #[test]
    fn size_limit_applies_to_every_format() {
        for route in ["/vega-lite", "/mermaid", "/svg", "/table"] {
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
