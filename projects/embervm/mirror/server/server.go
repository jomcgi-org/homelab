// Package server serves bare git mirrors over the smart HTTP protocol so a
// session guest can hydrate its workspace from a node-local copy instead of
// cloning GitHub through the credential-injecting egress lane (#4473).
//
// Only git-upload-pack is served, anonymous and read-only. There is no
// receive-pack: the claude runtime's hydration path never pushes to the
// mirror, and a read-only surface is the whole posture (ADR agents/050 kept
// scratch-ref pushes on the retired central mirror; nothing consumes them on
// this node-local one).
//
// The wire shape is the standard git smart-http contract:
//
//	GET  /<owner>/<repo>.git/info/refs?service=git-upload-pack
//	     -> application/x-git-upload-pack-advertisement
//	POST /<owner>/<repo>.git/git-upload-pack
//	     -> application/x-git-upload-pack-result
//
// backed by `git upload-pack` subprocesses against the on-disk bare clones,
// which keeps every protocol decision (v0/v2 negotiation, pack generation,
// blob filters) inside git rather than reimplemented here.
package server

import (
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// DefaultGitBin is the git binary used unless WithGitBin overrides it.
const DefaultGitBin = "git"

const (
	contentTypeAdvertisement = "application/x-git-upload-pack-advertisement"
	contentTypeRequest       = "application/x-git-upload-pack-request"
	contentTypeResult        = "application/x-git-upload-pack-result"
	serviceUploadPack        = "git-upload-pack"
)

// Server serves one mirrors root over git smart-http. It is safe for
// concurrent use: every request runs its own short-lived git subprocess.
type Server struct {
	// root holds one bare clone per mirrored repository, laid out as
	// <root>/<owner>/<repo>.git exactly as the URL path names them.
	root string
	// gitBin is the git executable. Overridable for tests.
	gitBin string
	logger *slog.Logger
}

// New builds a Server serving the bare clones under root.
func New(root string, logger *slog.Logger) *Server {
	if logger == nil {
		logger = slog.Default()
	}
	return &Server{root: root, gitBin: DefaultGitBin, logger: logger}
}

// WithGitBin overrides the git executable. Production leaves the default;
// tests pin the host git explicitly so the suite does not depend on PATH.
func (s *Server) WithGitBin(gitBin string) *Server {
	s.gitBin = gitBin
	return s
}

// Handler returns the http.Handler serving the mirror. /healthz answers 200 so
// an in-pod probe has something cheap to hit; everything else is the git
// smart-http surface.
func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(s.serveHTTP)
}

func (s *Server) serveHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "ok\n")
		return
	}
	repoDir, action, ok := resolveRepoPath(s.root, r.URL.Path)
	if !ok {
		http.NotFound(w, r)
		return
	}
	// An unmaterialized mirror (not yet cloned by the refresher, or a repo
	// that never existed) is a plain 404 for every action.
	if _, err := os.Stat(repoDir); err != nil {
		http.NotFound(w, r)
		return
	}
	switch action {
	case "info/refs":
		s.serveInfoRefs(w, r, repoDir)
	case "git-upload-pack":
		s.serveUploadPack(w, r, repoDir)
	default:
		http.NotFound(w, r)
	}
}

// resolveRepoPath maps "/<owner>/<repo>.git/<action>" onto
// <root>/<owner>/<repo>.git and returns the action suffix. The mapping is
// strict: exactly two non-empty path segments before the action, no dot-dot,
// no hidden segments, because the repo dir is joined onto root verbatim.
func resolveRepoPath(root, requestPath string) (repoDir, action string, ok bool) {
	trimmed := strings.TrimPrefix(requestPath, "/")
	owner, rest, found := strings.Cut(trimmed, "/")
	if !found {
		return "", "", false
	}
	name, action, found := strings.Cut(rest, "/")
	if !found {
		return "", "", false
	}
	for _, seg := range []string{owner, name} {
		if seg == "" || seg == "." || seg == ".." || strings.HasPrefix(seg, ".") {
			return "", "", false
		}
	}
	// GitHub's git endpoints are addressed with the .git suffix and the mirror
	// layout uses it too; requiring it here keeps plain directory names from
	// being served by accident.
	if !strings.HasSuffix(name, ".git") || name == ".git" {
		return "", "", false
	}
	action = strings.TrimSuffix(action, "/")
	return filepath.Join(root, owner, name), action, true
}

// serveInfoRefs answers the ref discovery request. Only upload-pack is
// offered: discovery for any other service (receive-pack) is a 404, so the
// mirror never even advertises a write side.
func (s *Server) serveInfoRefs(w http.ResponseWriter, r *http.Request, repoDir string) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", http.MethodGet+", "+http.MethodHead)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if service := r.URL.Query().Get("service"); service != serviceUploadPack {
		s.logger.Warn("mirror: refused non-upload-pack ref discovery", "service", service)
		http.NotFound(w, r)
		return
	}
	out, err := s.runUploadPack(r.Context(), repoDir, "--http-backend-info-refs", nil, protocolEnv(r))
	if err != nil {
		s.logger.Error("mirror: ref discovery failed", "err", err)
		http.Error(w, "ref discovery failed", http.StatusInternalServerError)
		return
	}
	body := append(serviceHeader(serviceUploadPack), out...)
	w.Header().Set("Content-Type", contentTypeAdvertisement)
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Content-Length", fmt.Sprint(len(body)))
	if r.Method == http.MethodHead {
		w.WriteHeader(http.StatusOK)
		return
	}
	_, _ = w.Write(body)
}

// serveUploadPack answers the stateless RPC POST by piping the request body
// through `git upload-pack --stateless-rpc` and streaming the pack back.
func (s *Server) serveUploadPack(w http.ResponseWriter, r *http.Request, repoDir string) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// The client content type is application/x-git-upload-pack-request;
	// anything else is not speaking the protocol at this endpoint.
	if ct := r.Header.Get("Content-Type"); !hasContentTypePrefix(ct, contentTypeRequest) {
		s.logger.Warn("mirror: refused upload-pack POST with unexpected content type", "content_type", ct)
		http.NotFound(w, r)
		return
	}
	body, err := decodeMaybeGzip(r)
	if err != nil {
		http.Error(w, "unreadable request body", http.StatusBadRequest)
		return
	}
	pr, pw := io.Pipe()
	cmdDone := make(chan error, 1)
	go func() {
		err := s.uploadPack(r.Context(), repoDir, "--stateless-rpc", body, pw, protocolEnv(r))
		_ = pw.CloseWithError(err)
		cmdDone <- err
	}()
	w.Header().Set("Content-Type", contentTypeResult)
	w.Header().Set("Cache-Control", "no-cache")
	flusher, _ := w.(http.Flusher)
	w.WriteHeader(http.StatusOK)
	buf := make([]byte, 32*1024)
	for {
		n, readErr := pr.Read(buf)
		if n > 0 {
			if _, writeErr := w.Write(buf[:n]); writeErr != nil {
				// Client hung up; the request context cancels the subprocess.
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			break
		}
	}
	if err := <-cmdDone; err != nil {
		// The response headers are already gone, so the truncated body is all
		// the client sees; log for the operator instead.
		s.logger.Error("mirror: upload-pack rpc failed", "err", err)
	}
}

func hasContentTypePrefix(ct, want string) bool {
	return ct == want || strings.HasPrefix(ct, want+";")
}

// decodeMaybeGzip unwraps a gzip request body. git compresses large
// upload-pack requests with Content-Encoding: gzip.
func decodeMaybeGzip(r *http.Request) (io.Reader, error) {
	if r.Header.Get("Content-Encoding") != "gzip" {
		return r.Body, nil
	}
	return gzip.NewReader(r.Body)
}

// serviceHeader renders the pkt-line "# service=<name>" banner every smart-http
// discovery response starts with, followed by the flush-pkt delimiter.
func serviceHeader(service string) []byte {
	line := fmt.Sprintf("# service=%s\n", service)
	var b bytes.Buffer
	fmt.Fprintf(&b, "%04x%s", len(line)+4, line)
	b.WriteString("0000")
	return b.Bytes()
}

// protocolEnv extracts the wire protocol version the client negotiated. Passing
// it through lets v2 clients stay on v2; absent or malformed values fall back
// to git's own default rather than being trusted blindly.
func protocolEnv(r *http.Request) string {
	for _, part := range strings.Split(r.Header.Get("Git-Protocol"), ":") {
		version, ok := strings.CutPrefix(part, "version=")
		if ok && isDigits(version) {
			return "GIT_PROTOCOL=" + part
		}
	}
	return ""
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// runUploadPack runs one buffered upload-pack invocation (ref discovery).
func (s *Server) runUploadPack(ctx context.Context, repoDir, mode string, stdin io.Reader, protocol string) ([]byte, error) {
	var out bytes.Buffer
	if err := s.uploadPack(ctx, repoDir, mode, stdin, &out, protocol); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

// uploadPack execs one `git upload-pack <mode> .` against repoDir. mode is
// --http-backend-info-refs for discovery or --stateless-rpc for the RPC POST.
//
// uploadpack.allowFilter here (not only in the repo config) is what makes
// --filter=blob:none clones work no matter how the bare clone was produced:
// partial clone support is the entire point of the mirror (#4473). A minimal
// environment keeps the guest-facing surface independent of whatever leaks in;
// GIT_PROTOCOL is appended only when the client sent a parseable version.
func (s *Server) uploadPack(ctx context.Context, repoDir, mode string, stdin io.Reader, stdout io.Writer, protocol string) error {
	cmd := exec.CommandContext(ctx, s.gitBin,
		"-c", "uploadpack.allowFilter=true",
		"upload-pack", mode, ".",
	)
	cmd.Dir = repoDir
	env := []string{"PATH=/usr/local/bin:/usr/bin:/bin"}
	if protocol != "" {
		env = append(env, protocol)
	}
	cmd.Env = env
	cmd.Stdin = stdin
	cmd.Stdout = stdout
	stderrBuf := &bytes.Buffer{}
	cmd.Stderr = stderrBuf
	if err := cmd.Run(); err != nil {
		if stderrText := strings.TrimSpace(stderrBuf.String()); stderrText != "" {
			return fmt.Errorf("upload-pack %s: %w: %s", mode, err, stderrText)
		}
		return fmt.Errorf("upload-pack %s: %w", mode, err)
	}
	return nil
}
