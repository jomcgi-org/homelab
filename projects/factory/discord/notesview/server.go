package notesview

import (
	"embed"
	"errors"
	"html/template"
	"io/fs"
	"log/slog"
	"net/http"
	"path"
	"sort"
	"strings"
	"unicode/utf8"
)

// CSP is the app's Content-Security-Policy. Scripts and styles load only
// from this host; no inline script runs, and the page connects nowhere.
// The edge sets the same header (notes-route.yaml).
const CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

const (
	maxQuery     = 200
	maxHits      = 50
	snippetRunes = 200
)

//go:embed templates/*.html
var templateFS embed.FS

//go:embed static
var staticFS embed.FS

var pages = template.Must(template.New("").Funcs(template.FuncMap{
	"noteURL": NoteURL,
	// dict2 passes the tree template its server, current note and folder.
	"dict2": func(server, current string, f *Folder) treeData {
		return treeData{Server: server, Current: current, Folder: f}
	},
	// folder is a note's folder, "" at the vault root.
	"folder": func(p string) string {
		if d := path.Dir(p); d != "." {
			return d
		}
		return ""
	},
}).ParseFS(templateFS, "templates/*.html"))

// Handler serves the viewer. The server comes only from the first path
// segment and must be allowlisted; nothing else names a prefix.
type Handler struct {
	Vault *Vault
	Log   *slog.Logger
	mux   *http.ServeMux
}

// NewHandler returns the viewer's routes.
func NewHandler(v *Vault, log *slog.Logger) *Handler {
	h := &Handler{Vault: v, Log: log, mux: http.NewServeMux()}
	static, _ := fs.Sub(staticFS, "static")
	files := http.FileServerFS(static)
	h.mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	h.mux.HandleFunc("GET /{server}", func(w http.ResponseWriter, r *http.Request) {
		if !v.Allowed(r.PathValue("server")) {
			notFound(w)
			return
		}
		http.Redirect(w, r, "/"+r.PathValue("server")+"/", http.StatusFound)
	})
	h.mux.HandleFunc("GET /{server}/{$}", h.withIndex(h.home))
	h.mux.HandleFunc("GET /{server}/n/{path...}", h.withIndex(h.note))
	h.mux.HandleFunc("GET /{server}/search", h.withIndex(h.search))
	h.mux.HandleFunc("GET /{server}/graph", h.withIndex(h.graph))
	h.mux.HandleFunc("GET /{server}/_static/{file}", func(w http.ResponseWriter, r *http.Request) {
		if !v.Allowed(r.PathValue("server")) {
			notFound(w)
			return
		}
		r2 := r.Clone(r.Context())
		r2.URL.Path = "/" + r.PathValue("file")
		w.Header().Set("Cache-Control", "public, max-age=300")
		files.ServeHTTP(w, r2)
	})
	h.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) { notFound(w) })
	return h
}

// ServeHTTP sets the security headers on every response, errors included.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	hd := w.Header()
	hd.Set("Content-Security-Policy", CSP)
	hd.Set("X-Content-Type-Options", "nosniff")
	hd.Set("Referrer-Policy", "no-referrer")
	hd.Set("Cross-Origin-Resource-Policy", "same-origin")
	hd.Set("X-Frame-Options", "DENY")
	hd.Set("Cache-Control", "no-store")
	h.mux.ServeHTTP(w, r)
}

func notFound(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusNotFound)
	_, _ = w.Write([]byte("not found\n"))
}

type indexed func(w http.ResponseWriter, r *http.Request, idx *Index)

func (h *Handler) withIndex(next indexed) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		server := r.PathValue("server")
		idx, err := h.Vault.Index(r.Context(), server)
		if errors.Is(err, ErrUnknownServer) {
			notFound(w)
			return
		}
		if err != nil {
			h.Log.Error("vault index", "server", server, "err", err)
			http.Error(w, "the notes are not readable now", http.StatusBadGateway)
			return
		}
		next(w, r, idx)
	}
}

// Folder is one folder of the file tree.
type Folder struct {
	Name    string
	Folders []*Folder
	Notes   []*Note
}

func tree(idx *Index) *Folder {
	root := &Folder{}
	for _, p := range idx.Paths {
		f := root
		parts := strings.Split(p, "/")
		for _, name := range parts[:len(parts)-1] {
			var next *Folder
			for _, c := range f.Folders {
				if c.Name == name {
					next = c
				}
			}
			if next == nil {
				next = &Folder{Name: name}
				f.Folders = append(f.Folders, next)
			}
			f = next
		}
		f.Notes = append(f.Notes, idx.Notes[p])
	}
	var sortAll func(*Folder)
	sortAll = func(f *Folder) {
		sort.Slice(f.Folders, func(i, j int) bool { return strings.ToLower(f.Folders[i].Name) < strings.ToLower(f.Folders[j].Name) })
		sort.Slice(f.Notes, func(i, j int) bool { return strings.ToLower(f.Notes[i].Name) < strings.ToLower(f.Notes[j].Name) })
		for _, c := range f.Folders {
			sortAll(c)
		}
	}
	sortAll(root)
	return root
}

type treeData struct {
	Server  string
	Current string
	Folder  *Folder
}

// page is the data every template gets.
type page struct {
	Server    string
	Title     string
	Tree      *Folder
	Count     int
	Truncated bool
	Current   string
	Note      *Note
	HTML      template.HTML
	Backlinks []*Note
	Query     string
	Hits      []hit
	Graph     *graphData
}

type hit struct {
	Note    *Note
	Snippet string
}

type graphNode struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	URL   string `json:"url"`
	Links int    `json:"links"`
}

type graphData struct {
	Nodes []graphNode `json:"nodes"`
	Edges [][2]int    `json:"edges"`
}

func (h *Handler) render(w http.ResponseWriter, name string, p page) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := pages.ExecuteTemplate(w, name, p); err != nil {
		h.Log.Error("render", "template", name, "err", err)
	}
}

func base(idx *Index, title string) page {
	return page{Server: idx.Server, Title: title, Tree: tree(idx), Count: len(idx.Paths), Truncated: idx.Truncated}
}

// homeNames are the notes a server's front page shows, first match wins.
var homeNames = []string{"index.md", "README.md", "Home.md"}

func (h *Handler) home(w http.ResponseWriter, _ *http.Request, idx *Index) {
	p := base(idx, "Notes")
	for _, name := range homeNames {
		if n := idx.Notes[name]; n != nil {
			p.Note, p.Current, p.HTML = n, n.Path, template.HTML(n.HTML) // #nosec G203 -- goldmark output with raw HTML off and URLs filtered (render.go)
			p.Backlinks = notesOf(idx, n.Backlinks)
			break
		}
	}
	h.render(w, "home.html", p)
}

func notesOf(idx *Index, paths []string) []*Note {
	out := make([]*Note, 0, len(paths))
	for _, p := range paths {
		out = append(out, idx.Notes[p])
	}
	return out
}

func (h *Handler) note(w http.ResponseWriter, r *http.Request, idx *Index) {
	rel := r.PathValue("path")
	n := idx.Notes[rel]
	if n == nil {
		n = idx.Notes[rel+".md"]
	}
	if n == nil {
		w.WriteHeader(http.StatusNotFound)
		p := base(idx, "Not found")
		h.render(w, "home.html", p)
		return
	}
	p := base(idx, n.Name)
	p.Note, p.Current = n, n.Path
	p.HTML = template.HTML(n.HTML) // #nosec G203 -- goldmark output with raw HTML off and URLs filtered (render.go)
	p.Backlinks = notesOf(idx, n.Backlinks)
	h.render(w, "note.html", p)
}

func (h *Handler) search(w http.ResponseWriter, r *http.Request, idx *Index) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if len(q) > maxQuery {
		q = q[:maxQuery]
		q = strings.ToValidUTF8(q, "")
	}
	p := base(idx, "Search")
	p.Query = q
	if q != "" {
		p.Hits = Search(idx, q)
	}
	h.render(w, "search.html", p)
}

// Search finds notes whose name, path or text holds q, ignoring case: name
// matches first, then by match count.
func Search(idx *Index, q string) []hit {
	lq := strings.ToLower(q)
	type scored struct {
		hit
		name  bool
		count int
	}
	var all []scored
	for _, p := range idx.Paths {
		n := idx.Notes[p]
		lower := strings.ToLower(n.Body)
		count := strings.Count(lower, lq)
		name := strings.Contains(strings.ToLower(p), lq)
		if count == 0 && !name {
			continue
		}
		all = append(all, scored{hit{n, snippet(n.Body, lower, lq)}, name, count})
	}
	sort.SliceStable(all, func(i, j int) bool {
		if all[i].name != all[j].name {
			return all[i].name
		}
		return all[i].count > all[j].count
	})
	out := make([]hit, 0, min(len(all), maxHits))
	for i := 0; i < len(all) && i < maxHits; i++ {
		out = append(out, all[i].hit)
	}
	return out
}

func snippet(body, lower, q string) string {
	runes := []rune(body)
	at := 0
	if i := strings.Index(lower, q); i >= 0 {
		at = utf8.RuneCountInString(lower[:i])
	}
	start := max(at-snippetRunes/2, 0)
	end := min(start+snippetRunes, len(runes))
	start = min(start, end)
	s := strings.Join(strings.Fields(string(runes[start:end])), " ")
	if start > 0 {
		s = "…" + s
	}
	if end < len(runes) {
		s += "…"
	}
	return s
}

func (h *Handler) graph(w http.ResponseWriter, _ *http.Request, idx *Index) {
	g := &graphData{Nodes: []graphNode{}, Edges: [][2]int{}}
	ids := map[string]int{}
	for i, p := range idx.Paths {
		n := idx.Notes[p]
		ids[p] = i
		g.Nodes = append(g.Nodes, graphNode{ID: i, Name: n.Name, URL: NoteURL(idx.Server, p), Links: len(n.Links) + len(n.Backlinks)})
	}
	for _, p := range idx.Paths {
		for _, to := range idx.Notes[p].Links {
			g.Edges = append(g.Edges, [2]int{ids[p], ids[to]})
		}
	}
	pg := base(idx, "Graph")
	pg.Graph = g
	h.render(w, "graph.html", pg)
}
