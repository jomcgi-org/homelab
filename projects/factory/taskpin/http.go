// Package taskpin transports a main commit from a task's dispatcher to its actor.
package taskpin

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
)

const (
	Path      = "/v1/task-analysis-pin"
	Audience  = "factory-task-pin"
	TokenFile = "/var/run/secrets/factory-task-pin/token"

	timeout = 4 * time.Second
)

var (
	commitID  = regexp.MustCompile(`^[0-9a-f]{40}$`)
	boundName = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,127}$`)
)

type request struct {
	Namespace string `json:"namespace"`
	Session   string `json:"session"`
}

type response struct {
	MainSHA string `json:"main_sha"`
}

type Verifier interface {
	Verify(context.Context, string) (actorjwt.Identity, error)
}

// Broker verifies the egress-injected actor token and sends only its identity
// to the dispatcher. The actor cannot supply a repository, commit or task ID.
func Broker(verifier Verifier, namespaces []string, owner string, token func() (string, error)) http.Handler {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	client := &http.Client{
		Transport: transport,
		Timeout:   timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	active := make(chan struct{}, 16)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", http.MethodGet)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if r.URL.RawQuery != "" || r.ContentLength != 0 {
			http.Error(w, "unexpected request parameters", http.StatusBadRequest)
			return
		}
		raw, ok := auth.BearerToken(r)
		if !ok || len(r.Header.Values("Authorization")) != 1 {
			http.Error(w, "actor token required", http.StatusUnauthorized)
			return
		}
		select {
		case active <- struct{}{}:
			defer func() { <-active }()
		default:
			http.Error(w, "task pin unavailable", http.StatusServiceUnavailable)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), timeout)
		defer cancel()
		id, err := verifier.Verify(ctx, raw)
		if err != nil {
			http.Error(w, "actor token rejected", http.StatusUnauthorized)
			return
		}
		session := id.SessionID()
		if !slices.Contains(namespaces, id.Atespace) || !boundName.MatchString(session) {
			http.Error(w, "actor has no analysis binding", http.StatusForbidden)
			return
		}
		status, pin := readOwner(ctx, client, owner, token, request{id.Atespace, session})
		if status != http.StatusOK {
			http.Error(w, "task pin unavailable", status)
			return
		}
		writePin(w, pin)
	})
}

// Owner serves one trusted dispatcher. Callers must wrap it in an
// audience-bound TokenReview check for the broker's ServiceAccount.
func Owner(resolve func(context.Context, string, string) (string, error)) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var input request
		if r.URL.RawQuery != "" || decode(http.MaxBytesReader(w, r.Body, 1024), &input) != nil ||
			!boundName.MatchString(input.Namespace) || !boundName.MatchString(input.Session) {
			http.Error(w, "invalid task identity", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), timeout)
		defer cancel()
		main, err := resolve(ctx, input.Namespace, input.Session)
		if errors.Is(err, sql.ErrNoRows) {
			http.Error(w, "active task pin unavailable", http.StatusNotFound)
			return
		}
		if err != nil || !commitID.MatchString(main) {
			http.Error(w, "task pin unavailable", http.StatusServiceUnavailable)
			return
		}
		writePin(w, response{main})
	})
}

func readOwner(ctx context.Context, client *http.Client, owner string, token func() (string, error), input request) (int, response) {
	credential, err := token()
	if err != nil || strings.TrimSpace(credential) == "" {
		return http.StatusServiceUnavailable, response{}
	}
	body, _ := json.Marshal(input)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, owner, bytes.NewReader(body))
	if err != nil {
		return http.StatusServiceUnavailable, response{}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(credential))
	res, err := client.Do(req)
	if err != nil {
		return http.StatusServiceUnavailable, response{}
	}
	defer res.Body.Close()
	if res.StatusCode == http.StatusNotFound {
		return http.StatusNotFound, response{}
	}
	var pin response
	data, err := io.ReadAll(io.LimitReader(res.Body, 1025))
	if res.StatusCode != http.StatusOK || err != nil || len(data) > 1024 ||
		decode(bytes.NewReader(data), &pin) != nil || !commitID.MatchString(pin.MainSHA) {
		return http.StatusServiceUnavailable, response{}
	}
	return http.StatusOK, pin
}

func decode(r io.Reader, target any) error {
	decoder := json.NewDecoder(r)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return errors.New("unexpected additional JSON data")
	}
	return nil
}

func writePin(w http.ResponseWriter, pin response) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(pin)
}
