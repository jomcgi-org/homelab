package github

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/prometheus/client_golang/prometheus/testutil"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

type mintRequest struct {
	Repositories []string          `json:"repositories"`
	Permissions  map[string]string `json:"permissions"`
}

// fakeGitHub fakes POST /app/installations/{id}/access_tokens, checking the
// App JWT and echoing the requested downscope.
type fakeGitHub struct {
	*httptest.Server
	mu       sync.Mutex
	requests []mintRequest
	serial   int
	widen    bool // answer with an extra repository
	noPerms  bool // answer without a permissions map
	fail     map[string]bool
	revoked  []string
}

func newFakeGitHub(t *testing.T, key *rsa.PrivateKey, appID, installation, owner string) *fakeGitHub {
	t.Helper()
	f := &fakeGitHub{fail: map[string]bool{}}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete && r.URL.Path == "/installation/token" {
			f.mu.Lock()
			f.revoked = append(f.revoked, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			f.mu.Unlock()
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if r.Method != http.MethodPost || r.URL.Path != "/app/installations/"+installation+"/access_tokens" {
			http.NotFound(w, r)
			return
		}
		if err := checkAppJWT(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), &key.PublicKey, appID); err != nil {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = fmt.Fprintf(w, `{"message":%q}`, err.Error())
			return
		}
		var req mintRequest
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &req); err != nil {
			w.WriteHeader(http.StatusUnprocessableEntity)
			return
		}
		f.mu.Lock()
		f.requests = append(f.requests, req)
		f.serial++
		serial, widen, noPerms := f.serial, f.widen, f.noPerms
		fail := len(req.Repositories) == 1 && f.fail[req.Repositories[0]]
		f.mu.Unlock()
		if fail {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"message":"There is at least one repository that does not exist or is not accessible"}`))
			return
		}
		repos := []map[string]string{}
		for _, name := range req.Repositories {
			repos = append(repos, map[string]string{"name": name, "full_name": owner + "/" + name})
		}
		if widen {
			repos = append(repos, map[string]string{"name": "other", "full_name": owner + "/other"})
		}
		perms := map[string]string{"metadata": "read"}
		for k, v := range req.Permissions {
			perms[k] = v
		}
		if noPerms {
			perms = nil
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"token": fmt.Sprintf("ghs_fake%d", serial), "expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
			"permissions": perms, "repositories": repos,
		})
	}))
	t.Cleanup(f.Close)
	return f
}

func checkAppJWT(token string, pub *rsa.PublicKey, appID string) error {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return fmt.Errorf("malformed JWT")
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return err
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(pub, crypto.SHA256, digest[:], sig); err != nil {
		return fmt.Errorf("bad signature")
	}
	payload, _ := base64.RawURLEncoding.DecodeString(parts[1])
	var claims struct {
		Iat, Exp int64
		Iss      json.Number
	}
	if err := json.Unmarshal(payload, &claims); err != nil {
		return err
	}
	now := time.Now().Unix()
	if claims.Iss.String() != appID || claims.Iat > now || claims.Exp <= now || claims.Exp-claims.Iat > 600 {
		return fmt.Errorf("bad claims %+v", claims)
	}
	return nil
}

func (f *fakeGitHub) lastRequests() []mintRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]mintRequest(nil), f.requests...)
}

func writeKey(t *testing.T, key *rsa.PrivateKey) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "private-key")
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

type harness struct {
	gh      *fakeGitHub
	client  *fake.Clientset
	metrics *metrics.Metrics
	minter  *Minter
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	rsaKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	key, err := LoadPrivateKey(writeKey(t, rsaKey))
	if err != nil {
		t.Fatal(err)
	}
	gh := newFakeGitHub(t, rsaKey, "12345", "678", "example-org")
	m := metrics.New()
	defaults := config.Defaults().GitHub
	client := fake.NewSimpleClientset()
	return &harness{gh: gh, client: client, metrics: m, minter: &Minter{
		App:    &App{APIURL: gh.URL, AppID: "12345", InstallationID: "678", Key: key, Client: gh.Client()},
		Client: client,
		Bindings: []config.Binding{
			{Name: "homelab", Repository: "example-org/homelab", Namespace: "factory-homelab"},
			{Name: "site", Repository: "example-org/site", Namespace: "factory-site"},
		},
		SecretName: "github-git", AgentPermissions: defaults.AgentPermissions, ConnectorPermissions: defaults.ConnectorPermissions,
		Interval: 30 * time.Minute, ConnectorMinLife: 15 * time.Minute, Metrics: m, Logger: slog.Default(),
	}}
}

func (h *harness) secret(t *testing.T, namespace string) *corev1.Secret {
	t.Helper()
	s, err := h.client.CoreV1().Secrets(namespace).Get(context.Background(), "github-git", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestMintAgentsDownscopesAndWritesSecrets(t *testing.T) {
	h := newHarness(t)
	if err := h.minter.MintAgents(context.Background()); err != nil {
		t.Fatal(err)
	}
	reqs := h.gh.lastRequests()
	if len(reqs) != 2 {
		t.Fatalf("mint requests = %d", len(reqs))
	}
	for i, repo := range []string{"homelab", "site"} {
		if len(reqs[i].Repositories) != 1 || reqs[i].Repositories[0] != repo || len(reqs[i].Permissions) != 1 || reqs[i].Permissions["contents"] != "write" {
			t.Fatalf("request %d = %+v, want only %s with contents:write", i, reqs[i], repo)
		}
	}
	s := h.secret(t, "factory-homelab")
	if s.Labels[InjectableLabel] != "true" || s.Annotations[ExpiresAnnotation] == "" {
		t.Fatalf("secret metadata = %v %v", s.Labels, s.Annotations)
	}
	decoded, err := base64.StdEncoding.DecodeString(string(s.Data[BasicKey]))
	if err != nil || string(decoded) != "x-access-token:ghs_fake1" || len(s.Data) != 1 {
		t.Fatalf("basic = %q (%v), data keys %d", decoded, err, len(s.Data))
	}

	// A second round updates the existing Secret in place, keeping labels.
	if err := h.minter.MintAgents(context.Background()); err != nil {
		t.Fatal(err)
	}
	s = h.secret(t, "factory-homelab")
	decoded, _ = base64.StdEncoding.DecodeString(string(s.Data[BasicKey]))
	if string(decoded) != "x-access-token:ghs_fake3" || s.Labels[InjectableLabel] != "true" {
		t.Fatalf("after remint basic = %q labels %v", decoded, s.Labels)
	}
	if got := testutil.ToFloat64(h.metrics.GitHubMints.WithLabelValues("agent", "homelab", "ok")); got != 2 {
		t.Fatalf("agent mint metric = %v", got)
	}
}

func TestReviewerMinterIsReadOnlyForAgentsAndLabelsItsMints(t *testing.T) {
	h := newHarness(t)
	review := config.Defaults().GitHubReview
	h.minter.AgentPermissions, h.minter.ConnectorPermissions = review.AgentPermissions, review.ConnectorPermissions
	h.minter.AgentKind, h.minter.TokenKind = "review_agent", "reviewer"
	if err := h.minter.MintAgents(context.Background()); err != nil {
		t.Fatal(err)
	}
	for i, req := range h.gh.lastRequests() {
		if len(req.Permissions) != 1 || req.Permissions["contents"] != "read" {
			t.Fatalf("agent request %d permissions = %v, want contents:read only", i, req.Permissions)
		}
	}
	tok, err := h.minter.ConnectorToken(context.Background(), "example-org/homelab")
	if err != nil {
		t.Fatal(err)
	}
	if tok.Permissions["pull_requests"] != "read" || tok.Permissions["checks"] != "write" || tok.Permissions["merge_queues"] != "write" {
		t.Fatalf("reviewer token permissions = %v", tok.Permissions)
	}
	if got := testutil.ToFloat64(h.metrics.GitHubMints.WithLabelValues("review_agent", "homelab", "ok")); got != 1 {
		t.Fatalf("review_agent mint metric = %v", got)
	}
	if got := testutil.ToFloat64(h.metrics.GitHubMints.WithLabelValues("reviewer", "homelab", "ok")); got != 1 {
		t.Fatalf("reviewer mint metric = %v", got)
	}
}

func TestMintAgentsRefusesUnmanagedSecret(t *testing.T) {
	h := newHarness(t)
	if _, err := h.client.CoreV1().Secrets("factory-homelab").Create(context.Background(), &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: "github-git", Namespace: "factory-homelab"},
		Data:       map[string][]byte{"other": []byte("keep")},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := h.minter.MintAgents(context.Background()); err == nil || !strings.Contains(err.Error(), "refusing") {
		t.Fatalf("MintAgents error = %v, want a refusal", err)
	}
	if s := h.secret(t, "factory-homelab"); string(s.Data["other"]) != "keep" || len(s.Data) != 1 {
		t.Fatalf("unmanaged Secret was changed: %v", s.Data)
	}
	if s := h.secret(t, "factory-site"); s.Labels[ManagedByLabel] != ManagedBy {
		t.Fatalf("managed Secret labels = %v", s.Labels)
	}
}

func TestMintAgentsIsolatesFailures(t *testing.T) {
	h := newHarness(t)
	h.gh.fail["homelab"] = true
	if err := h.minter.MintAgents(context.Background()); err == nil || !strings.Contains(err.Error(), "homelab") {
		t.Fatalf("MintAgents error = %v", err)
	}
	if _, err := h.client.CoreV1().Secrets("factory-site").Get(context.Background(), "github-git", metav1.GetOptions{}); err != nil {
		t.Fatalf("healthy binding not written: %v", err)
	}
	if got := testutil.ToFloat64(h.metrics.GitHubMints.WithLabelValues("agent", "homelab", "error")); got != 1 {
		t.Fatalf("error metric = %v", got)
	}
}

func TestMintRejectsAndRevokesWrongScope(t *testing.T) {
	h := newHarness(t)
	h.gh.widen = true
	if _, err := h.minter.App.Mint(context.Background(), "example-org/homelab", map[string]string{"contents": "write"}); err == nil {
		t.Fatal("accepted a token scoped to more than one repository")
	}
	h.gh.mu.Lock()
	h.gh.widen, h.gh.noPerms = false, true
	h.gh.mu.Unlock()
	if _, err := h.minter.App.Mint(context.Background(), "example-org/homelab", map[string]string{"contents": "write"}); err == nil {
		t.Fatal("accepted a token reporting no permissions")
	}
	h.gh.mu.Lock()
	revoked := append([]string(nil), h.gh.revoked...)
	h.gh.mu.Unlock()
	if len(revoked) != 2 || revoked[0] != "ghs_fake1" || revoked[1] != "ghs_fake2" {
		t.Fatalf("revoked tokens = %v, want both rejected tokens", revoked)
	}
	if err := checkScope("o/r", map[string]string{"contents": "write"}, map[string]string{"contents": "read", "metadata": "read"}, true); err == nil {
		t.Fatal("accepted a lower level than requested")
	}
	if err := checkScope("o/r", map[string]string{"contents": "write"}, map[string]string{"contents": "write", "issues": "write"}, true); err == nil {
		t.Fatal("accepted an unrequested permission")
	}
	if _, err := h.minter.App.Mint(context.Background(), "homelab", map[string]string{"contents": "write"}); err == nil {
		t.Fatal("accepted a repository without an owner")
	}
}

func TestConnectorToken(t *testing.T) {
	h := newHarness(t)
	handler := h.minter.ConnectorHandler()
	get := func(repo string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/github/connector-token?repository="+repo, nil))
		return rec
	}
	rec := get("example-org/homelab")
	if rec.Code != http.StatusOK || rec.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("connector token = %d %s", rec.Code, rec.Body)
	}
	var body struct {
		Token       string            `json:"token"`
		Repository  string            `json:"repository"`
		Permissions map[string]string `json:"permissions"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body.Token != "ghs_fake1" || body.Repository != "example-org/homelab" || body.Permissions["pull_requests"] != "write" {
		t.Fatalf("connector body = %+v", body)
	}
	req := h.gh.lastRequests()[0]
	want := config.Defaults().GitHub.ConnectorPermissions
	if len(req.Repositories) != 1 || req.Repositories[0] != "homelab" || len(req.Permissions) != len(want) {
		t.Fatalf("connector mint request = %+v", req)
	}
	for k, v := range want {
		if req.Permissions[k] != v {
			t.Fatalf("connector permission %s = %q, want %q", k, req.Permissions[k], v)
		}
	}
	// Cached while it has life left.
	if rec := get("example-org/homelab"); rec.Code != http.StatusOK || len(h.gh.lastRequests()) != 1 {
		t.Fatalf("second request reminted: %d requests", len(h.gh.lastRequests()))
	}
	if rec := get("example-org/unbound"); rec.Code != http.StatusNotFound {
		t.Fatalf("unbound repository = %d", rec.Code)
	}
}
