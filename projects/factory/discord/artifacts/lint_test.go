package artifacts

import (
	"strings"
	"testing"
)

const housePage = `<!doctype html><html><head>
<link rel="stylesheet" href="/_lib/house-2.css" />
<style>.note { color: var(--muted); }</style>
</head><body><main class="page"><h1>Plain</h1></main></body></html>`

func TestLintPassesTheHousePages(t *testing.T) {
	pages := map[string]string{"template": pageTemplate, "house-2": housePage}
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
		`<style>.card { --bg: #fff; }</style>`,
	} {
		if w := lintHTML(housePage + style); len(w) != 0 {
			t.Errorf("%s: unexpected warnings %q", style, w)
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
