package notesview

import (
	"bytes"
	"net/url"
	"regexp"
	"strings"

	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/extension"
	"github.com/yuin/goldmark/parser"
	"github.com/yuin/goldmark/renderer"
	"github.com/yuin/goldmark/renderer/html"
	"github.com/yuin/goldmark/text"
	"github.com/yuin/goldmark/util"
)

// Notes are untrusted text: Bosun writes them from prompts that hold other
// people's Discord messages. goldmark runs without html.WithUnsafe, so raw
// HTML becomes an "omitted" comment and text is escaped. Every link and image
// URL must also pass safeURL: relative, http, https or mailto, never
// javascript:, data: or any other scheme.

// KindWikiLink is the AST kind of a [[wikilink]].
var KindWikiLink = ast.NewNodeKind("WikiLink")

// WikiLink is [[Target]], [[Target|Label]] or [[Target#Heading|Label]].
type WikiLink struct {
	ast.BaseInline
	Target string // as written, without #heading and |label
	Label  string
}

// Kind implements ast.Node.
func (n *WikiLink) Kind() ast.NodeKind { return KindWikiLink }

// Dump implements ast.Node.
func (n *WikiLink) Dump(source []byte, level int) {
	ast.DumpHelper(n, source, level, map[string]string{"Target": n.Target, "Label": n.Label}, nil)
}

type wikiParser struct{}

func (wikiParser) Trigger() []byte { return []byte{'['} }

func (wikiParser) Parse(_ ast.Node, block text.Reader, _ parser.Context) ast.Node {
	line, _ := block.PeekLine()
	if !bytes.HasPrefix(line, []byte("[[")) {
		return nil
	}
	end := bytes.Index(line[2:], []byte("]]"))
	if end <= 0 || end > 300 {
		return nil
	}
	inner := string(line[2 : 2+end])
	if strings.ContainsAny(inner, "[]\n\r") {
		return nil
	}
	block.Advance(end + 4)
	target, label, _ := strings.Cut(inner, "|")
	target, _, _ = strings.Cut(target, "#")
	target = strings.TrimSpace(target)
	label = strings.TrimSpace(label)
	if label == "" {
		label = strings.TrimSpace(inner)
		if t, _, ok := strings.Cut(label, "|"); ok {
			label = strings.TrimSpace(t)
		}
	}
	return &WikiLink{Target: target, Label: label}
}

// Resolver maps a wikilink target to a note path ("" when no note has it).
type Resolver func(target string) string

// linkRenderer writes wikilinks; html/template is not in this path, so it
// escapes every value itself.
type linkRenderer struct {
	resolve Resolver
	href    func(path string) string
}

func (r *linkRenderer) RegisterFuncs(reg renderer.NodeRendererFuncRegisterer) {
	reg.Register(KindWikiLink, func(w util.BufWriter, _ []byte, node ast.Node, entering bool) (ast.WalkStatus, error) {
		if !entering {
			return ast.WalkContinue, nil
		}
		n := node.(*WikiLink)
		label := util.EscapeHTML([]byte(n.Label))
		if p := r.resolve(n.Target); p != "" {
			_, _ = w.WriteString(`<a class="wikilink" href="`)
			_, _ = w.Write(util.EscapeHTML([]byte(r.href(p))))
			_, _ = w.WriteString(`">`)
			_, _ = w.Write(label)
			_, _ = w.WriteString(`</a>`)
		} else {
			_, _ = w.WriteString(`<span class="wikilink unresolved" title="No note with this name">`)
			_, _ = w.Write(label)
			_, _ = w.WriteString(`</span>`)
		}
		return ast.WalkSkipChildren, nil
	})
}

var scheme = regexp.MustCompile(`^[a-z][a-z0-9+.-]*:`)

// safeURL allows relative URLs and http, https and mailto. Browsers drop
// ASCII tab and newline inside a URL and trim leading control characters
// and spaces, so the check does the same before it reads the scheme.
func safeURL(u []byte) bool {
	// The destination is still raw here: the renderer resolves entities
	// and backslash escapes later, so the check resolves them first.
	u = util.ResolveEntityNames(util.ResolveNumericReferences(util.UnescapePunctuations(u)))
	s := strings.Map(func(r rune) rune {
		if r <= ' ' || r == 0x7f {
			return -1
		}
		return r
	}, strings.ToLower(string(u)))
	if s == "" {
		return true
	}
	if strings.HasPrefix(s, "\\") || strings.HasPrefix(s, "/\\") {
		return false
	}
	m := scheme.FindString(s)
	if m == "" {
		// A ':' before any '/', '?' or '#' would be a scheme to a browser.
		if i := strings.IndexAny(s, ":/?#"); i >= 0 && s[i] == ':' {
			return false
		}
		return true
	}
	return m == "http:" || m == "https:" || m == "mailto:"
}

// urlFilter clears every unsafe link or image destination.
type urlFilter struct{}

func (urlFilter) Transform(doc *ast.Document, reader text.Reader, _ parser.Context) {
	var drop []*ast.AutoLink
	_ = ast.Walk(doc, func(n ast.Node, entering bool) (ast.WalkStatus, error) {
		if !entering {
			return ast.WalkContinue, nil
		}
		switch v := n.(type) {
		case *ast.Link:
			if !safeURL(v.Destination) {
				v.Destination = nil
			}
		case *ast.Image:
			if !safeURL(v.Destination) {
				v.Destination = nil
			}
		case *ast.AutoLink:
			if !safeURL(v.URL(reader.Source())) {
				drop = append(drop, v)
			}
		}
		return ast.WalkContinue, nil
	})
	// Shown as text, not as a link; replaced after the walk.
	for _, v := range drop {
		v.Parent().ReplaceChild(v.Parent(), v, ast.NewString(v.Label(reader.Source())))
	}
}

// Rendered is one note as HTML, with the wikilink targets it names.
type Rendered struct {
	HTML    string
	Targets []string
}

// Render turns one note's Markdown into safe HTML. resolve maps a wikilink
// target to a note path; href maps a note path to its URL.
func Render(src []byte, resolve Resolver, href func(string) string) (Rendered, error) {
	md := goldmark.New(
		goldmark.WithExtensions(extension.GFM),
		goldmark.WithParserOptions(
			parser.WithInlineParsers(util.Prioritized(wikiParser{}, 199)),
			parser.WithASTTransformers(util.Prioritized(urlFilter{}, 1000)),
		),
		goldmark.WithRendererOptions(
			// Unsafe stays off: raw HTML is omitted.
			html.WithHardWraps(),
			renderer.WithNodeRenderers(util.Prioritized(&linkRenderer{resolve: resolve, href: href}, 100)),
		),
	)
	doc := md.Parser().Parse(text.NewReader(src))
	var out Rendered
	_ = ast.Walk(doc, func(n ast.Node, entering bool) (ast.WalkStatus, error) {
		if w, ok := n.(*WikiLink); ok && entering {
			out.Targets = append(out.Targets, w.Target)
		}
		return ast.WalkContinue, nil
	})
	var buf bytes.Buffer
	if err := md.Renderer().Render(&buf, src, doc); err != nil {
		return Rendered{}, err
	}
	out.HTML = buf.String()
	return out, nil
}

// NoteURL is the viewer URL of a note path under /<server>/n/.
func NoteURL(server, path string) string {
	segs := strings.Split(path, "/")
	for i, s := range segs {
		segs[i] = url.PathEscape(s)
	}
	return "/" + server + "/n/" + strings.Join(segs, "/")
}
