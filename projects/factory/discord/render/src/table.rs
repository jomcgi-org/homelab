// The /table route: a table as JSON becomes an SVG in the house theme.
// The renderer writes every element itself and escapes all text, so the
// drawing holds no reference to anything outside it.

use serde_json::Value;

const MAX_COLUMNS: usize = 12;
const MAX_ROWS: usize = 100;
const MAX_CELL_CHARS: usize = 1000;

const FONT: &str = "Inter, 'Noto Color Emoji', 'DejaVu Sans', sans-serif";
const SIZE: f64 = 14.0;
const LINE: f64 = 20.0;
const PAD_X: f64 = 14.0;
const PAD_Y: f64 = 10.0;
const MARGIN: f64 = 12.0;
// A cell wraps at this text width.
const MAX_TEXT: f64 = 300.0;

#[derive(Clone, Copy, PartialEq, Debug)]
enum Align {
    Left,
    Center,
    Right,
}

struct Table {
    columns: Vec<String>,
    align: Vec<Align>,
    rows: Vec<Vec<String>>,
}

fn strings(value: &Value) -> Option<Vec<String>> {
    value
        .as_array()?
        .iter()
        .map(|cell| {
            let text = cell.as_str()?;
            (text.chars().count() <= MAX_CELL_CHARS).then(|| text.to_string())
        })
        .collect()
}

fn parse(body: &[u8]) -> Option<Table> {
    let value: Value = serde_json::from_slice(body).ok()?;
    let object = value.as_object()?;
    if object
        .keys()
        .any(|key| !matches!(key.as_str(), "columns" | "align" | "rows"))
    {
        return None;
    }
    let columns = strings(object.get("columns")?)?;
    if columns.is_empty() || columns.len() > MAX_COLUMNS {
        return None;
    }
    let align = match object.get("align") {
        None => vec![Align::Left; columns.len()],
        Some(value) => strings(value)?
            .iter()
            .map(|name| match name.as_str() {
                "left" | "" => Some(Align::Left),
                "center" => Some(Align::Center),
                "right" => Some(Align::Right),
                _ => None,
            })
            .collect::<Option<Vec<_>>>()?,
    };
    let rows = object
        .get("rows")?
        .as_array()?
        .iter()
        .map(strings)
        .collect::<Option<Vec<_>>>()?;
    if align.len() != columns.len()
        || rows.len() > MAX_ROWS
        || rows.iter().any(|row| row.len() != columns.len())
    {
        return None;
    }
    Some(Table {
        columns,
        align,
        rows,
    })
}

// width estimates a character's advance in Inter at SIZE px. It errs wide,
// so text stays inside its cell.
fn width(c: char) -> f64 {
    let em = match c {
        '\u{200D}' | '\u{FE00}'..='\u{FE0F}' | '\u{0300}'..='\u{036F}' => 0.0,
        '\u{1F000}'..='\u{1FAFF}'
        | '\u{2600}'..='\u{27BF}'
        | '\u{2B00}'..='\u{2BFF}'
        | '\u{1100}'..='\u{115F}'
        | '\u{2E80}'..='\u{A4CF}'
        | '\u{AC00}'..='\u{D7A3}'
        | '\u{F900}'..='\u{FAFF}'
        | '\u{FF00}'..='\u{FF60}' => 1.3,
        ' ' => 0.28,
        'i' | 'l' | 'j' | '.' | ',' | ':' | ';' | '\'' | '|' | '!' => 0.32,
        'f' | 'r' | 't' | 'I' | '(' | ')' | '[' | ']' | '-' => 0.42,
        'm' | 'w' | 'M' | 'W' => 0.9,
        'A'..='Z' => 0.7,
        '0'..='9' => 0.62,
        c if c.is_ascii() => 0.58,
        _ => 0.7,
    };
    em * SIZE
}

fn text_width(text: &str) -> f64 {
    text.chars().map(width).sum()
}

// wrap breaks text into lines no wider than MAX_TEXT: at spaces where it
// can, inside a word where it must. A newline in the text always breaks.
fn wrap(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    for paragraph in text.split('\n') {
        let mut line = String::new();
        for word in paragraph.split(' ').filter(|word| !word.is_empty()) {
            let candidate = if line.is_empty() {
                word.to_string()
            } else {
                format!("{line} {word}")
            };
            if text_width(&candidate) <= MAX_TEXT {
                line = candidate;
                continue;
            }
            if !line.is_empty() {
                lines.push(std::mem::take(&mut line));
            }
            for c in word.chars() {
                if text_width(&line) + width(c) > MAX_TEXT && !line.is_empty() {
                    lines.push(std::mem::take(&mut line));
                }
                line.push(c);
            }
        }
        lines.push(line);
    }
    lines
}

fn escape(text: &str) -> String {
    text.chars()
        .filter(|c| !c.is_control())
        .map(|c| match c {
            '&' => "&amp;".to_string(),
            '<' => "&lt;".to_string(),
            '>' => "&gt;".to_string(),
            '"' => "&quot;".to_string(),
            '\'' => "&apos;".to_string(),
            c => c.to_string(),
        })
        .collect()
}

// svg returns the drawing for a table spec, or None for a spec it refuses.
pub fn svg(body: &[u8]) -> Option<String> {
    let table = parse(body)?;
    let header: Vec<Vec<String>> = table.columns.iter().map(|cell| wrap(cell)).collect();
    let rows: Vec<Vec<Vec<String>>> = table
        .rows
        .iter()
        .map(|row| row.iter().map(|cell| wrap(cell)).collect())
        .collect();
    let widths: Vec<f64> = (0..table.columns.len())
        .map(|column| {
            std::iter::once(&header[column])
                .chain(rows.iter().map(|row| &row[column]))
                .flatten()
                .map(|line| text_width(line))
                .fold(0.0, f64::max)
                .max(SIZE)
                + 2.0 * PAD_X
        })
        .collect();
    let height = |cells: &[Vec<String>]| {
        cells.iter().map(Vec::len).max().unwrap_or(1) as f64 * LINE + 2.0 * PAD_Y
    };
    let table_width: f64 = widths.iter().sum();
    let header_height = height(&header);
    let row_heights: Vec<f64> = rows.iter().map(|row| height(row)).collect();
    let table_height = header_height + row_heights.iter().sum::<f64>();
    let (w, h) = (table_width + 2.0 * MARGIN, table_height + 2.0 * MARGIN);

    let mut out = format!(
        r##"<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0}" height="{h:.0}" viewBox="0 0 {w:.0} {h:.0}"><rect width="100%" height="100%" fill="#FFFFFF"/><defs><clipPath id="card"><rect x="{MARGIN}" y="{MARGIN}" width="{table_width:.1}" height="{table_height:.1}" rx="10"/></clipPath></defs><g clip-path="url(#card)" font-family="{FONT}" font-size="{SIZE}"><rect x="{MARGIN}" y="{MARGIN}" width="{table_width:.1}" height="{header_height:.1}" fill="#F1F5F9"/>"##
    );
    let mut y = MARGIN + header_height;
    for (index, row_height) in row_heights.iter().enumerate() {
        if index % 2 == 1 {
            out += &format!(
                r##"<rect x="{MARGIN}" y="{y:.1}" width="{table_width:.1}" height="{row_height:.1}" fill="#F8FAFC"/>"##
            );
        }
        out += &format!(
            r##"<rect x="{MARGIN}" y="{y:.1}" width="{table_width:.1}" height="1" fill="#E2E8F0"/>"##
        );
        y += row_height;
    }
    let mut draw = |cells: &[Vec<String>], top: f64, header: bool| {
        let mut x = MARGIN;
        for (column, lines) in cells.iter().enumerate() {
            let (anchor, at) = match table.align[column] {
                Align::Left => ("start", x + PAD_X),
                Align::Center => ("middle", x + widths[column] / 2.0),
                Align::Right => ("end", x + widths[column] - PAD_X),
            };
            let (weight, color) = if header {
                ("600", "#334155")
            } else {
                ("400", "#0F172A")
            };
            for (n, line) in lines.iter().enumerate() {
                let baseline = top + PAD_Y + LINE * n as f64 + 15.0;
                out += &format!(
                    r#"<text x="{at:.1}" y="{baseline:.1}" text-anchor="{anchor}" font-weight="{weight}" fill="{color}">{}</text>"#,
                    escape(line)
                );
            }
            x += widths[column];
        }
    };
    draw(&header, MARGIN, true);
    let mut top = MARGIN + header_height;
    for (row, row_height) in rows.iter().zip(&row_heights) {
        draw(row, top, false);
        top += row_height;
    }
    out += &format!(
        r##"</g><rect x="{MARGIN}" y="{MARGIN}" width="{table_width:.1}" height="{table_height:.1}" rx="10" fill="none" stroke="#CBD5E1"/></svg>"##
    );
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn draws_an_escaped_table() {
        let spec = r#"{"columns":["Tool","Status"],"align":["left","center"],"rows":[["<b>x</b> & y","✅"]]}"#;
        let drawing = svg(spec.as_bytes()).unwrap();
        assert!(drawing.contains("&lt;b&gt;x&lt;/b&gt; &amp; y"));
        assert!(drawing.contains("✅"));
        assert!(!drawing.contains("<b>"));
        assert!(drawing.contains(r#"text-anchor="middle""#));
    }

    #[test]
    fn refuses_malformed_tables() {
        for spec in [
            r#"{"columns":[],"rows":[]}"#,
            r#"{"columns":["a"],"rows":[["1","2"]]}"#,
            r#"{"columns":["a"],"rows":[[1]]}"#,
            r#"{"columns":["a"],"align":["up"],"rows":[]}"#,
            r#"{"columns":["a"],"rows":[],"href":"x"}"#,
            r#"["a"]"#,
        ] {
            assert!(svg(spec.as_bytes()).is_none(), "{spec}");
        }
    }

    #[test]
    fn long_cells_wrap() {
        let long = "word ".repeat(80);
        assert!(wrap(&long).len() > 3);
        assert!(wrap(&"x".repeat(200)).len() > 1);
        assert_eq!(wrap("a\nb"), vec!["a", "b"]);
    }
}
