package notesview

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"

	"golang.org/x/net/html"

	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

const (
	homelab = "1501965852042330302"
	other   = "1512814732392927463"
)

// fakeBucket is a read-only bucket in key order. It records every key a
// call names, so a test can prove the viewer read no other server.
type fakeBucket struct {
	objects map[string]string
	keys    []string // every Get key and List prefix
	page    int      // max keys per list page (0: 1000)
}

func (b *fakeBucket) Get(_ context.Context, key string) ([]byte, error) {
	b.keys = append(b.keys, key)
	v, ok := b.objects[key]
	if !ok {
		return nil, artifacts.ErrNotFound
	}
	return []byte(v), nil
}

// ListPage pages by key: the token is the last key of the page before.
func (b *fakeBucket) ListPage(_ context.Context, prefix, from string, limit int) ([]artifacts.Object, string, error) {
	b.keys = append(b.keys, prefix)
	if b.page > 0 {
		limit = min(limit, b.page)
	}
	var all []string
	for k := range b.objects {
		if strings.HasPrefix(k, prefix) && k > from {
			all = append(all, k)
		}
	}
	sort.Strings(all)
	next := ""
	if len(all) > limit {
		all = all[:limit]
		next = all[limit-1]
	}
	out := make([]artifacts.Object, 0, len(all))
	for _, k := range all {
		out = append(out, artifacts.Object{Key: k, Size: int64(len(b.objects[k]))})
	}
	return out, next, nil
}

func vault(objects map[string]string) (*Vault, *fakeBucket) {
	b := &fakeBucket{objects: objects}
	return &Vault{Bucket: b, Servers: map[string]bool{homelab: true, other: true}, TTL: time.Minute}, b
}

func get(t *testing.T, h http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
	return w
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestHiddenFoldersNeverAppear(t *testing.T) {
	v, _ := vault(map[string]string{
		homelab + "/Plans.md":                          "see [[Secret]]",
		homelab + "/_hermes/MEMORY.md":                 "memory",
		homelab + "/.trash/Secret.md":                  "trashed text",
		homelab + "/.history/Plans/20261128T1.md":      "old plans",
		homelab + "/(draft) idea.md":                   "paren first",
		homelab + "/zeta.md":                           "last",
		homelab + "/notes.txt":                         "not markdown",
		other + "/Other.md":                            "other server",
		"1290668195249917964/Outside.md":               "not allowlisted here",
		homelab + "/folder/.hidden.md":                 "dot file",
		homelab + "/.history/zzz/20261128T1.md":        "old",
		homelab + "/.trash/(draft) idea 20261128.md":   "old",
		homelab + "/.history/(draft) idea/20261128.md": "old",
	})
	idx, err := v.Index(context.Background(), homelab)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"(draft) idea.md", "Plans.md", "_hermes/MEMORY.md", "zeta.md"}
	if strings.Join(idx.Paths, "|") != strings.Join(want, "|") {
		t.Fatalf("paths = %q, want %q", idx.Paths, want)
	}
	h := NewHandler(v, quiet())
	for _, p := range []string{"/.trash/Secret.md", "/.history/Plans/20261128T1.md", "/folder/.hidden.md"} {
		if w := get(t, h, "/"+homelab+"/n"+p); w.Code != http.StatusNotFound {
			t.Errorf("%s: status %d, want 404", p, w.Code)
		}
	}
	// A link to a trashed note does not resolve.
	if body := get(t, h, "/"+homelab+"/n/Plans.md").Body.String(); !strings.Contains(body, `wikilink unresolved`) {
		t.Errorf("link to a trashed note resolved: %s", body)
	}
	for _, p := range []string{"/" + homelab + "/", "/" + homelab + "/search?q=old", "/" + homelab + "/graph"} {
		body := get(t, h, p).Body.String()
		for _, leak := range []string{"trashed text", "old plans", ".trash", ".history", "Secret.md", "dot file"} {
			if strings.Contains(body, leak) {
				t.Errorf("%s shows %q", p, leak)
			}
		}
	}
}

func TestListingPagesPastHiddenKeys(t *testing.T) {
	objects := map[string]string{homelab + "/b.md": "b", homelab + "/(a).md": "a"}
	for i := range 50 {
		objects[homelab+"/.history/b/"+time.Unix(int64(i), 0).UTC().Format("150405")+".md"] = "x"
	}
	v, b := vault(objects)
	b.page = 7
	idx, err := v.Index(context.Background(), homelab)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(idx.Paths, "|") != "(a).md|b.md" || idx.Truncated {
		t.Fatalf("paths = %q, truncated %v", idx.Paths, idx.Truncated)
	}
	// With more pages than maxPages, the index says it is cut short.
	b.page = 1
	v.cache = nil
	if idx, err = v.Index(context.Background(), homelab); err != nil || !idx.Truncated {
		t.Fatalf("truncated = %v, err %v", idx != nil && idx.Truncated, err)
	}
}

func TestServerComesOnlyFromThePath(t *testing.T) {
	v, b := vault(map[string]string{
		homelab + "/Home.md":            "homelab note",
		other + "/Home.md":              "other note",
		"1290668195249917964/Secret.md": "not allowlisted",
	})
	h := NewHandler(v, quiet())
	for _, p := range []string{
		"/1290668195249917964/",
		"/1290668195249917964/n/Secret.md",
		"/notaguild/",
		"/../" + other + "/",
		"/" + homelab + "x/",
		"/_static/notes.css",
		"/",
	} {
		// The mux answers a dot segment with a redirect to the clean path.
		if w := get(t, h, p); w.Code != http.StatusNotFound && w.Code != http.StatusTemporaryRedirect {
			t.Errorf("%s: status %d, want 404", p, w.Code)
		}
	}
	// A query or path inside the server cannot reach another prefix.
	for _, p := range []string{
		"/" + homelab + "/n/../" + other + "/Home.md",
		"/" + homelab + "/n/%2e%2e/" + other + "/Home.md",
		"/" + homelab + "/search?q=other&server=" + other,
	} {
		w := get(t, h, p)
		if strings.Contains(w.Body.String(), "other note") {
			t.Errorf("%s served another server's note", p)
		}
	}
	for _, k := range b.keys {
		if !strings.HasPrefix(k, homelab+"/") {
			t.Errorf("read key %q outside the homelab prefix", k)
		}
	}
	if w := get(t, h, "/"+homelab); w.Code != http.StatusFound || w.Header().Get("Location") != "/"+homelab+"/" {
		t.Errorf("/%s: %d %q", homelab, w.Code, w.Header().Get("Location"))
	}
}

func TestWikilinksResolveLikeObsidianWithBacklinks(t *testing.T) {
	v, _ := vault(map[string]string{
		homelab + "/Home.md":               "Start at [[plans]] and [[Trips/Skye|the Skye trip]], [[Skye#Day 2]], [[Missing]] and `[[code]]`.",
		homelab + "/Plans.md":              "Back to [[home]].",
		homelab + "/Trips/Skye.md":         "Skye trip, see [[Plans]].",
		homelab + "/Archive/Old/Skye.md":   "older Skye",
		homelab + "/Trips/With space.md":   "[[With Space]]",
		homelab + "/Trips/Ünïcode note.md": "x",
	})
	idx, err := v.Index(context.Background(), homelab)
	if err != nil {
		t.Fatal(err)
	}
	home := idx.Notes["Home.md"]
	if strings.Join(home.Links, "|") != "Plans.md|Trips/Skye.md" {
		t.Errorf("Home links = %q", home.Links)
	}
	if got := strings.Join(idx.Notes["Plans.md"].Backlinks, "|"); got != "Home.md|Trips/Skye.md" {
		t.Errorf("Plans backlinks = %q", got)
	}
	// The shortest path wins for a bare name.
	if got := idx.Resolve("SKYE"); got != "Trips/Skye.md" {
		t.Errorf("Resolve(SKYE) = %q", got)
	}
	if got := idx.Resolve("archive/old/skye.md"); got != "Archive/Old/Skye.md" {
		t.Errorf("Resolve(path) = %q", got)
	}
	for _, want := range []string{
		`<a class="wikilink" href="/` + homelab + `/n/Plans.md">plans</a>`,
		`<a class="wikilink" href="/` + homelab + `/n/Trips/Skye.md">the Skye trip</a>`,
		`<a class="wikilink" href="/` + homelab + `/n/Trips/Skye.md">Skye#Day 2</a>`,
		`<span class="wikilink unresolved" title="No note with this name">Missing</span>`,
		`<code>[[code]]</code>`,
	} {
		if !strings.Contains(home.HTML, want) {
			t.Errorf("Home HTML lacks %s:\n%s", want, home.HTML)
		}
	}
	h := NewHandler(v, quiet())
	body := get(t, h, "/"+homelab+"/n/Plans.md").Body.String()
	if !strings.Contains(body, "Linked from") || !strings.Contains(body, `href="/`+homelab+`/n/Home.md"`) {
		t.Errorf("Plans page lacks backlinks:\n%s", body)
	}
	// URL-escaped and bare paths both find a note.
	for _, p := range []string{"/n/Trips/With%20space.md", "/n/Trips/With%20space", "/n/Trips/%C3%9Cn%C3%AFcode%20note.md"} {
		if w := get(t, h, "/"+homelab+p); w.Code != http.StatusOK {
			t.Errorf("%s: %d", p, w.Code)
		}
	}
	// The front page shows Home.md.
	if body := get(t, h, "/"+homelab+"/").Body.String(); !strings.Contains(body, "Start at") {
		t.Errorf("front page lacks Home.md")
	}
}

// The XSS corpus: notes are untrusted text, so every one renders inert.
var xss = []string{
	`<script>alert(1)</script>`,
	`<img src=x onerror=alert(1)>`,
	`<svg onload=alert(1)>`,
	`<iframe src="https://evil.example"></iframe>`,
	`<a href="javascript:alert(1)">x</a>`,
	`[click](javascript:alert(1))`,
	`[click](JaVaScRiPt:alert(1))`,
	`[click](java&#x09;script:alert(1))`,
	"[click](java\tscript:alert(1))",
	`[click]( javascript:alert(1))`,
	`[click](&#106;avascript:alert(1))`,
	`[click](vbscript:msgbox(1))`,
	`[click](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)`,
	`![img](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)`,
	`![img](data:image/png;base64,iVBORw0KGgo=)`,
	`![x](javascript:alert(1))`,
	`<javascript:alert(1)>`,
	`[ref]` + "\n\n" + `[ref]: javascript:alert(1)`,
	`[x](\\evil.example)`,
	`[x](x:alert(1))`,
	`[[<script>alert(1)</script>]]`,
	`[[Target|<img src=x onerror=alert(1)>]]`,
	`[[" onmouseover="alert(1)]]`,
	`<div style="background:url(javascript:alert(1))">x</div>`,
	"```html\n<script>alert(1)</script>\n```",
	`<style>body{display:none}</style>`,
	`<form action="https://evil.example"><input></form>`,
	`<meta http-equiv="refresh" content="0;url=https://evil.example">`,
	`<base href="https://evil.example/">`,
	`[x](https://ok.example "title\" onmouseover=\"alert(1)")`,
}

// allowedTags are the elements goldmark's GFM output may hold.
var allowedTags = map[string]bool{
	"p": true, "a": true, "span": true, "code": true, "pre": true, "em": true, "strong": true,
	"del": true, "ul": true, "ol": true, "li": true, "blockquote": true, "br": true, "hr": true,
	"h1": true, "h2": true, "h3": true, "h4": true, "h5": true, "h6": true, "img": true,
	"table": true, "thead": true, "tbody": true, "tr": true, "th": true, "td": true, "input": true,
}

// inert parses rendered HTML as a browser would and returns why it is not
// inert, or "".
func inert(server, fragment string) string {
	z := html.NewTokenizer(strings.NewReader(fragment))
	for {
		switch z.Next() {
		case html.ErrorToken:
			return ""
		case html.StartTagToken, html.SelfClosingTagToken:
			tok := z.Token()
			if !allowedTags[tok.Data] {
				return "tag " + tok.Data
			}
			for _, a := range tok.Attr {
				name := strings.ToLower(a.Key)
				switch {
				case strings.HasPrefix(name, "on"), name == "style", name == "srcdoc", name == "formaction":
					return "attribute " + name
				case name == "href" || name == "src":
					u := strings.ToLower(a.Val)
					if u != "" && !strings.HasPrefix(u, "/"+server+"/n/") && !strings.HasPrefix(u, "https://") {
						return name + "=" + a.Val
					}
				}
				if tok.Data == "input" && name == "type" && a.Val != "checkbox" {
					return "input type " + a.Val
				}
			}
		}
	}
}

func TestXSSCorpusRendersInert(t *testing.T) {
	objects := map[string]string{homelab + "/Target.md": "target"}
	for i, s := range xss {
		objects[homelab+"/x"+string(rune('a'+i%26))+string(rune('a'+i/26))+".md"] = s
	}
	v, _ := vault(objects)
	h := NewHandler(v, quiet())
	idx, err := v.Index(context.Background(), homelab)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range idx.Paths {
		n := idx.Notes[p]
		w := get(t, h, NoteURL(homelab, p))
		if w.Code != http.StatusOK {
			t.Fatalf("%s: %d", p, w.Code)
		}
		page := w.Body.String()
		// Only the page's own markup: the article body.
		start := strings.Index(page, `<div class="markdown">`) + len(`<div class="markdown">`)
		end := strings.Index(page, `</article>`)
		body := strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(page[start:end]), "</div>"))
		if why := inert(homelab, body); why != "" {
			t.Errorf("%q rendered %s:\n%s", n.Body, why, body)
		}
		// Every page's scripts come from this host only.
		for _, m := range regexp.MustCompile(`<script[^>]*>`).FindAllString(page, -1) {
			if m != `<script id="graph-data" type="application/json">` && m != `<script src="/`+homelab+`/_static/graph.js">` {
				t.Errorf("unexpected script tag %s", m)
			}
		}
	}
}

func TestGraphDataIsEscapedJSON(t *testing.T) {
	v, _ := vault(map[string]string{
		homelab + "/</script><script>alert(1)</script>.md": "x",
		homelab + "/A.md": "[[B]]",
		homelab + "/B.md": "b",
	})
	h := NewHandler(v, quiet())
	body := get(t, h, "/"+homelab+"/graph").Body.String()
	if strings.Count(body, "</script>") != 2 || strings.Contains(body, "<script>alert") {
		t.Fatalf("graph data broke out of its block:\n%s", body)
	}
	if !strings.Contains(body, `"edges":[[0,1]]`) && !strings.Contains(body, `"edges":[[1,2]]`) {
		t.Errorf("graph lacks the A->B edge:\n%s", body)
	}
}

func TestSecurityHeadersAndNoCookies(t *testing.T) {
	v, _ := vault(map[string]string{homelab + "/A.md": "a"})
	h := NewHandler(v, quiet())
	for _, p := range []string{"/" + homelab + "/", "/" + homelab + "/n/A.md", "/" + homelab + "/n/missing", "/nope/", "/" + homelab + "/_static/notes.css", "/" + homelab + "/_static/graph.js"} {
		w := get(t, h, p)
		hd := w.Header()
		if hd.Get("Content-Security-Policy") != CSP || hd.Get("X-Content-Type-Options") != "nosniff" || hd.Get("Referrer-Policy") != "no-referrer" {
			t.Errorf("%s: headers %v", p, hd)
		}
		if hd.Get("Set-Cookie") != "" {
			t.Errorf("%s sets a cookie", p)
		}
	}
	for _, d := range []string{"default-src 'none'", "script-src 'self'", "connect-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"} {
		if !strings.Contains(CSP, d) {
			t.Errorf("CSP lacks %s", d)
		}
	}
	if strings.Contains(CSP, "unsafe") {
		t.Errorf("CSP allows unsafe sources: %s", CSP)
	}
	if ct := get(t, h, "/"+homelab+"/_static/graph.js").Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/javascript") {
		t.Errorf("graph.js Content-Type %q", ct)
	}
	// Only GET and HEAD.
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/"+homelab+"/", strings.NewReader("x")))
	if w.Code == http.StatusOK {
		t.Errorf("POST answered 200")
	}
}

func TestSearchFindsNamesAndText(t *testing.T) {
	v, _ := vault(map[string]string{
		homelab + "/Pub quiz.md": "Teams of four",
		homelab + "/Food.md":     "The quiz night has chips. Quiz again.",
		homelab + "/Other.md":    "nothing",
	})
	h := NewHandler(v, quiet())
	body := get(t, h, "/"+homelab+"/search?q=QUIZ").Body.String()
	body = body[strings.Index(body, "<main>"):]
	pub, food := strings.Index(body, "Pub quiz</a>"), strings.Index(body, "Food</a>")
	if pub < 0 || food < 0 || pub > food || strings.Contains(body, "Other</a>") {
		t.Errorf("search order or hits wrong:\n%s", body)
	}
	if body := get(t, h, "/"+homelab+"/search?q=%3Cscript%3E").Body.String(); strings.Contains(body, "<script>") {
		t.Errorf("query echoed unescaped")
	}
}

func TestIndexIsCachedForTTL(t *testing.T) {
	v, b := vault(map[string]string{homelab + "/A.md": "a"})
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	v.Now = func() time.Time { return now }
	ctx := context.Background()
	if _, err := v.Index(ctx, homelab); err != nil {
		t.Fatal(err)
	}
	b.objects[homelab+"/B.md"] = "b"
	idx, _ := v.Index(ctx, homelab)
	if len(idx.Paths) != 1 {
		t.Errorf("rebuilt inside the TTL")
	}
	now = now.Add(2 * time.Minute)
	idx, _ = v.Index(ctx, homelab)
	if len(idx.Paths) != 2 {
		t.Errorf("not rebuilt after the TTL")
	}
}

func TestSafeURL(t *testing.T) {
	for u, want := range map[string]bool{
		"":                        true,
		"other.md":                true,
		"../x/y.md":               true,
		"#heading":                true,
		"?q=1":                    true,
		"https://example.com":     true,
		"HTTP://example.com":      true,
		"mailto:a@example.com":    true,
		"//example.com/x":         true,
		"javascript:alert(1)":     false,
		" javascript:alert(1)":    false,
		"java\tscript:alert(1)":   false,
		"java\nscript:alert(1)":   false,
		"\x01javascript:alert(1)": false,
		"data:text/html,x":        false,
		"data:image/png;base64,":  false,
		"vbscript:x":              false,
		"file:///etc/passwd":      false,
		"x:y":                     false,
		"\\\\evil.example":        false,
		"/\\evil.example":         false,
	} {
		if got := safeURL([]byte(u)); got != want {
			t.Errorf("safeURL(%q) = %v, want %v", u, got, want)
		}
	}
}

// The checker itself catches what it must.
func TestInertCatchesActiveMarkup(t *testing.T) {
	for _, s := range []string{
		`<script>x</script>`,
		`<img src="x" onerror="y">`,
		`<a href="javascript:x">x</a>`,
		`<a href="data:text/html,x">x</a>`,
		`<p style="x">x</p>`,
		`<input type="text">`,
	} {
		if inert(homelab, s) == "" {
			t.Errorf("inert(%q) found nothing", s)
		}
	}
}
