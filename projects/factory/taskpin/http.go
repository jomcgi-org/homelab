// Package taskpin transports a main commit from a task's dispatcher to its actor.
package taskpin

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
)

const (
	Path = "/v1/task-analysis-pin"

	audience = "factory-broker"
	caFile   = "/run/substrate-servicedns/trust-bundle.pem"
	timeout  = 4 * time.Second
)

var (
	commitID  = regexp.MustCompile(`^[0-9a-f]{40}$`)
	boundName = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,127}$`)
)

type actor struct {
	namespace, session, token string
}

type response struct {
	MainSHA string `json:"main_sha"`
}

type Verifier interface {
	Verify(context.Context, string) (actorjwt.Identity, error)
}

// Broker routes each verified actor only to its configured dispatcher.
func Broker(verifier Verifier, owners map[string]string) http.Handler {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	client := &http.Client{
		Transport: transport,
		Timeout:   timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	return actorRequest(verifier, func(ctx context.Context, a actor) (response, int) {
		owner := owners[a.namespace]
		if owner == "" {
			return response{}, http.StatusForbidden
		}
		return readOwner(ctx, client, owner, a.token)
	})
}

// NewOwnerServer gives the dispatcher an independent actor JWT check. It needs
// the public issuer CA and never takes a caller-supplied task or repository.
func NewOwnerServer(ctx context.Context, issuer string, resolve func(context.Context, string, string) (string, error), log *slog.Logger) (*http.Server, error) {
	verifier, err := actorjwt.New(actorjwt.Options{Issuer: issuer, Audience: audience, CAFile: caFile, Logger: log})
	if err != nil {
		return nil, err
	}
	go verifier.Run(ctx)
	mux := http.NewServeMux()
	mux.Handle(Path, ownerHandler(verifier, resolve))
	return &http.Server{
		Addr: ":8086", Handler: mux, ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout: 5 * time.Second, WriteTimeout: 6 * time.Second,
	}, nil
}

func ownerHandler(verifier Verifier, resolve func(context.Context, string, string) (string, error)) http.Handler {
	return actorRequest(verifier, func(ctx context.Context, a actor) (response, int) {
		main, err := resolve(ctx, a.namespace, a.session)
		if errors.Is(err, sql.ErrNoRows) {
			return response{}, http.StatusNotFound
		}
		if err != nil || !commitID.MatchString(main) {
			return response{}, http.StatusServiceUnavailable
		}
		return response{main}, http.StatusOK
	})
}

func actorRequest(verifier Verifier, lookup func(context.Context, actor) (response, int)) http.Handler {
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
		if !boundName.MatchString(id.Atespace) || !boundName.MatchString(session) {
			http.Error(w, "actor has no task session", http.StatusForbidden)
			return
		}
		pin, status := lookup(ctx, actor{id.Atespace, session, raw})
		if status != http.StatusOK {
			http.Error(w, "task pin unavailable", status)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(pin)
	})
}

func readOwner(ctx context.Context, client *http.Client, owner, token string) (response, int) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, owner, nil)
	if err != nil {
		return response{}, http.StatusServiceUnavailable
	}
	req.Header.Set("Authorization", "Bearer "+token)
	res, err := client.Do(req)
	if err != nil {
		return response{}, http.StatusServiceUnavailable
	}
	defer res.Body.Close()
	if res.StatusCode == http.StatusNotFound {
		return response{}, http.StatusNotFound
	}
	data, err := io.ReadAll(io.LimitReader(res.Body, 1025))
	if res.StatusCode != http.StatusOK || err != nil || len(data) > 1024 {
		return response{}, http.StatusServiceUnavailable
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var pin response
	if err := decoder.Decode(&pin); err != nil || !commitID.MatchString(pin.MainSHA) || decoder.Decode(new(any)) != io.EOF {
		return response{}, http.StatusServiceUnavailable
	}
	return pin, http.StatusOK
}
