package artifacts

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const housePage = `<!doctype html><html><head>
<link rel="stylesheet" href="/_lib/house-2.css" />
<style>.note { color: var(--muted); }</style>
</head><body><main class="page"><h1>Plain</h1></main></body></html>`

func TestLintPassesTheHousePages(t *testing.T) {
	pages := map[string]string{"template": pageTemplate, "house-2": housePage}
	if path := os.Getenv("SELFTEST_PAGE"); path != "" {
		// A rootpath is relative to the workspace root in the runfiles.
		if !filepath.IsAbs(path) {
			path = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), path)
		}
		page, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		pages["selftest/architecture.html"] = string(page)
	}
	for _, name := range exampleNames {
		page, err := examplePages.ReadFile("examples/" + name + ".html")
		if err != nil {
			t.Fatal(err)
		}
		pages[name] = string(page)
	}
	for name, page := range pages {
		if w := lintHTML(page); len(w) != 0 {
			t.Errorf("%s: unexpected warnings %q", name, w)
		}
	}
}

func TestLintWarnsWithoutTheHouseStylesheet(t *testing.T) {
	for _, page := range []string{
		`<html><head><style>body{margin:0}</style></head><body>x</body></html>`,
		`<link rel="stylesheet" href="https://cdn.example.com/house-1.css">`,
		`<link rel="icon" href="/_lib/house-2.css">`,
		`<!-- <link rel="stylesheet" href="/_lib/house-2.css"> -->`,
		`<link rel="stylesheet" data-href="/_lib/house-2.css">`,
		`<link rel="stylesheet" href="/_lib/house-x.css">`,
	} {
		w := lintHTML(page)
		if len(w) != 1 || !strings.Contains(w[0], "house stylesheet") {
			t.Errorf("%q: warnings %q", page, w)
		}
	}
}

func TestLintNamesEmoji(t *testing.T) {
	page := housePage + "<p>Done \U0001F680 ✅ \U0001F680 ⭐ \U0001F525 \U0001F389 \U0001F44D</p>"
	w := lintHTML(page)
	if len(w) != 1 || !strings.Contains(w[0], "emoji") {
		t.Fatalf("warnings %q", w)
	}
	// Five distinct emoji, in page order, each once.
	if !strings.Contains(w[0], "(\U0001F680 ✅ ⭐ \U0001F525 \U0001F389)") {
		t.Errorf("named emoji: %q", w[0])
	}
}

func TestLintWarnsOnAPrivatePalette(t *testing.T) {
	cases := map[string]string{
		"--bg, --text, --accent":     `<style>:root { --bg: #000; --text: white; --accent: var(--x); --gap: 4px; }</style>`,
		"--card-background, --brand": `<style>:root:not([data-theme="light"]) { --card-background: #fff; --brand: oklch(70% 0.1 200); }</style>`,
		"--ink":                      `<style>@media (prefers-color-scheme: dark) { :root { --ink: #eee; } }</style>`,
	}
	for want, style := range cases {
		w := lintHTML(housePage + style)
		if len(w) != 1 || !strings.Contains(w[0], "("+want+")") {
			t.Errorf("%s: warnings %q", want, w)
		}
	}
	for _, style := range []string{
		`<style>:root { --gap: 4px; --text-size: 18px; }</style>`,
		`<style>.card { --bg: var(--surface); }</style>`,
	} {
		if w := lintHTML(housePage + style); len(w) != 0 {
			t.Errorf("%s: unexpected warnings %q", style, w)
		}
	}
}

func TestTheTemplateLinksTheNewestHouse(t *testing.T) {
	// LIB_NAMES lists the files projects/factory/discord/libs serves.
	names := strings.Fields(os.Getenv("LIB_NAMES"))
	if len(names) == 0 {
		t.Skip("LIB_NAMES is set only under Bazel")
	}
	newest := 0
	for _, name := range names {
		if n := houseVersion("/_lib/" + name); n > newest {
			newest = n
		}
	}
	if newest == 0 || newestHouse != newest {
		t.Errorf("the template links house-%d; the libs serve house-%d", newestHouse, newest)
	}
}

func TestLintWarnsOnAnOldHouseVersion(t *testing.T) {
	old := fmt.Sprintf("house-%d", newestHouse-1)
	page := strings.ReplaceAll(pageTemplate, fmt.Sprintf("house-%d", newestHouse), old)
	w := lintHTML(page)
	want := fmt.Sprintf("(%s.css, %s.js); the newest is house-%d", old, old, newestHouse)
	if len(w) != 1 || !strings.Contains(w[0], want) {
		t.Errorf("warnings %q, want one naming %q", w, want)
	}
}

func TestLintNamesColourLiterals(t *testing.T) {
	cases := map[string]string{
		// A style block, with a hex fallback, rgb() and a nested rule.
		"#e5e7eb, rgba(…)": `<style>.story { border-top: 1px solid var(--line, #e5e7eb); }
@media (min-width: 40rem) { .x:hover { background: rgba(0, 0, 0, 0.1); } }</style>`,
		// Style and SVG paint attributes.
		"#FFF, hsl(…), #2a78d6": `<p style="color:#FFF;background:hsl(0 0% 50%)">x</p><svg><rect fill="#2a78d6" stroke="currentColor"/></svg>`,
		// A chart spec: a condition and a scale range, not the data.
		"#2A78D6, #9CA3AF, #EDA100": `<script type="application/json" class="vega-lite">{"data":{"values":[{"k":"#123","n":1}]},
"mark":"bar","encoding":{"color":{"condition":{"test":"datum.k=='a'","value":"#2A78D6"},"value":"#9CA3AF"},
"fill":{"scale":{"range":["#2A78D6","#EDA100"]}}}}</script>`,
		// A Mermaid style line.
		"#DBEAFE": "<pre class=\"mermaid\">flowchart TD\n  A --> B\n  style A fill:#DBEAFE\n</pre>",
	}
	for want, extra := range cases {
		w := lintHTML(housePage + extra)
		if len(w) != 1 || !strings.Contains(w[0], "("+want+")") {
			t.Errorf("%s: warnings %q", want, w)
		}
	}
	for _, extra := range []string{
		// An id selector, a fragment link, an SVG url() and an entity.
		`<style>#add { color: var(--accent); } .g { fill: url(#fade); }</style><a href="#add">&#123;</a>`,
		// Data labels in a chart and a Mermaid node label.
		`<script type="application/json" class="vega-lite">{"data":{"values":[{"pr":"#8805"}]},"mark":"bar"}</script>`,
		// Issue numbers outside colour keys: a sort order, a domain and a filter.
		`<script type="application/json" class="vega-lite">{"mark":"bar","transform":[{"filter":{"field":"pr","equal":"#8805"}}],
"encoding":{"x":{"field":"pr","sort":["#8805","#8807"],"scale":{"domain":["#101","#202"]}}}}</script>`,
		"<pre class=\"mermaid\">flowchart TD\n  A[PR #8805] --> B\n</pre>",
		`<svg><path fill="none" stroke="currentColor"/></svg>`,
	} {
		if w := lintHTML(housePage + extra); len(w) != 0 {
			t.Errorf("%s: unexpected warnings %q", extra, w)
		}
	}
}

func TestPutReturnsWarningsAndStoresTheFile(t *testing.T) {
	srv, fake := setup(t)
	put := func(path, content string) []any {
		t.Helper()
		res := call(t, srv.URL, "put", map[string]any{"server": guild, "channel": channel, "path": path, "content": content})
		if res["isError"] != false {
			t.Fatalf("put %s failed: %v", path, res)
		}
		if string(fake.objects[guild+"/"+channel+"/"+path].body) != content {
			t.Fatalf("put %s did not store the file", path)
		}
		w, _ := res["structuredContent"].(map[string]any)["warnings"].([]any)
		return w
	}
	if w := put("off.html", "<style>:root{--bg:#000}</style><p>\U0001F680</p>"); len(w) != 3 {
		t.Errorf("off-house page: warnings %v, want 3", w)
	}
	if w := put("house.html", housePage); len(w) != 0 {
		t.Errorf("house page: warnings %v", w)
	}
	if w := put("notes.md", "# Notes \U0001F680\n:root{--bg:#000}"); len(w) != 0 {
		t.Errorf("markdown: warnings %v", w)
	}
}
