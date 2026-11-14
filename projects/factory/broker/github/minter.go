package github

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const (
	// InjectableLabel lets Substrate's Kubernetes-Secret credential
	// provider read the Secret for gateway injection.
	InjectableLabel = "substrate.ate.dev/injectable"
	// ExpiresAnnotation records the token's expiry on the Secret.
	ExpiresAnnotation = "factory.jomcgi.dev/token-expires-at"
	// BasicKey holds base64("x-access-token:<token>"), the value of an
	// HTTP Basic Authorization header for git over HTTPS.
	BasicKey = "basic"
)

// Minter keeps each binding's agent git Secret current and serves connector
// tokens.
type Minter struct {
	App                  *App
	Client               kubernetes.Interface
	Bindings             []config.Binding
	SecretName           string
	AgentPermissions     map[string]string
	ConnectorPermissions map[string]string
	Interval             time.Duration
	// RetryInterval replaces Interval after a failed round.
	RetryInterval time.Duration
	// ConnectorMinLife is the least remaining lifetime of a cached
	// connector token; an older one is reminted.
	ConnectorMinLife time.Duration
	Metrics          *metrics.Metrics
	Logger           *slog.Logger

	mu        sync.Mutex
	connector map[string]Token
}

// Run mints every binding's agent token now and then every Interval, sooner
// after a failure, until ctx ends.
func (m *Minter) Run(ctx context.Context) {
	for {
		wait := m.Interval
		if err := m.MintAgents(ctx); err != nil {
			m.Logger.Error("GitHub agent token round failed", "err", err)
			if m.RetryInterval > 0 && m.RetryInterval < wait {
				wait = m.RetryInterval
			}
		}
		t := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-t.C:
		}
	}
}

// MintAgents mints one agent token per binding and writes it to the
// binding's namespace. One binding's failure does not stop the others.
func (m *Minter) MintAgents(ctx context.Context) error {
	var errs []error
	for _, b := range m.Bindings {
		tok, err := m.App.Mint(ctx, b.Repository, m.AgentPermissions)
		if err == nil {
			err = m.writeSecret(ctx, b, tok)
		}
		outcome := "ok"
		if err != nil {
			outcome = "error"
			errs = append(errs, fmt.Errorf("binding %s: %w", b.Name, err))
		} else {
			m.Logger.Info("GitHub agent token written", "binding", b.Name, "namespace", b.Namespace, "expires_at", tok.ExpiresAt.UTC().Format(time.RFC3339))
		}
		m.Metrics.GitHubMints.WithLabelValues("agent", b.Name, outcome).Inc()
	}
	return errors.Join(errs...)
}

func (m *Minter) writeSecret(ctx context.Context, b config.Binding, tok Token) error {
	secrets := m.Client.CoreV1().Secrets(b.Namespace)
	value := []byte(base64.StdEncoding.EncodeToString([]byte("x-access-token:" + tok.Token)))
	expires := tok.ExpiresAt.UTC().Format(time.RFC3339)
	current, err := secrets.Get(ctx, m.SecretName, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		_, err = secrets.Create(ctx, &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name: m.SecretName, Namespace: b.Namespace,
				Labels:      map[string]string{InjectableLabel: "true", "app.kubernetes.io/managed-by": "factory-broker"},
				Annotations: map[string]string{ExpiresAnnotation: expires},
			},
			Type: corev1.SecretTypeOpaque,
			Data: map[string][]byte{BasicKey: value},
		}, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	if current.Labels == nil {
		current.Labels = map[string]string{}
	}
	if current.Annotations == nil {
		current.Annotations = map[string]string{}
	}
	current.Labels[InjectableLabel] = "true"
	current.Annotations[ExpiresAnnotation] = expires
	current.Data = map[string][]byte{BasicKey: value}
	_, err = secrets.Update(ctx, current, metav1.UpdateOptions{})
	return err
}

// ConnectorToken returns a connector token for a bound repository, reusing
// a cached one while it has ConnectorMinLife left.
func (m *Minter) ConnectorToken(ctx context.Context, repository string) (Token, error) {
	var binding *config.Binding
	for i := range m.Bindings {
		if m.Bindings[i].Repository == repository {
			binding = &m.Bindings[i]
		}
	}
	if binding == nil {
		return Token{}, errUnbound
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if tok, ok := m.connector[repository]; ok && time.Until(tok.ExpiresAt) > m.ConnectorMinLife {
		return tok, nil
	}
	tok, err := m.App.Mint(ctx, repository, m.ConnectorPermissions)
	outcome := "ok"
	if err != nil {
		outcome = "error"
	}
	m.Metrics.GitHubMints.WithLabelValues("connector", binding.Name, outcome).Inc()
	if err != nil {
		return Token{}, err
	}
	if m.connector == nil {
		m.connector = map[string]Token{}
	}
	m.connector[repository] = tok
	return tok, nil
}

var errUnbound = errors.New("repository has no binding")

// ConnectorHandler serves GET /github/connector-token?repository=owner/name.
// Callers wrap it with TokenReview authorization.
func (m *Minter) ConnectorHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		tok, err := m.ConnectorToken(r.Context(), r.URL.Query().Get("repository"))
		switch {
		case errors.Is(err, errUnbound):
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(map[string]string{"factory_error": "repository_not_bound"})
			return
		case err != nil:
			m.Logger.Error("GitHub connector token failed", "err", err)
			w.WriteHeader(http.StatusBadGateway)
			_ = json.NewEncoder(w).Encode(map[string]string{"factory_error": "github_mint_failed"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"token": tok.Token, "expires_at": tok.ExpiresAt.UTC().Format(time.RFC3339),
			"repository": tok.Repository, "permissions": tok.Permissions,
		})
	})
}
