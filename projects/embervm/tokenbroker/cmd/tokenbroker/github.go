package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider/githubapp"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
)

type githubGrantConfig struct {
	Name             string   `json:"name"`
	Profile          string   `json:"profile"`
	RepositoryIDs    []int64  `json:"repositoryIDs"`
	AllowedSPIFFEIDs []string `json:"allowedSpiffeIds"`
}

type githubGrant struct {
	minter  provider.Minter
	callers map[string]bool
	profile string
}

func configuredGitHubGrants(listeners listenerConfig) (map[string]githubGrant, error) {
	raw := os.Getenv("GITHUB_APP_GRANTS")
	if raw == "" {
		return nil, nil
	}
	if listeners.tlsListenAddr == "" {
		return nil, errors.New("GitHub grants require the SPIFFE mTLS listener")
	}
	var configs []githubGrantConfig
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&configs); err != nil {
		return nil, errors.New("invalid GITHUB_APP_GRANTS")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, errors.New("GITHUB_APP_GRANTS must contain one JSON array")
	}
	if len(configs) == 0 {
		return nil, errors.New("GITHUB_APP_GRANTS must contain an explicit grant")
	}
	appID, err := strconv.ParseInt(os.Getenv("GITHUB_APP_ID"), 10, 64)
	if err != nil || appID <= 0 {
		return nil, errors.New("GITHUB_APP_ID must be positive")
	}
	installationID, err := strconv.ParseInt(os.Getenv("GITHUB_APP_INSTALLATION_ID"), 10, 64)
	if err != nil || installationID <= 0 {
		return nil, errors.New("GITHUB_APP_INSTALLATION_ID must be positive")
	}
	key, err := githubapp.ParsePrivateKey([]byte(os.Getenv("GITHUB_APP_PRIVATE_KEY")))
	if err != nil {
		return nil, err
	}
	listenerIDs := map[string]bool{}
	for _, id := range listeners.spiffeClientIDs {
		listenerIDs[id.String()] = true
	}
	grants := map[string]githubGrant{}
	namePattern := regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
	for _, config := range configs {
		if _, exists := grants[config.Name]; exists || !namePattern.MatchString(config.Name) {
			return nil, errors.New("GitHub grant names must be unique lowercase identifiers")
		}
		if len(config.AllowedSPIFFEIDs) == 0 {
			return nil, fmt.Errorf("GitHub grant %s requires explicit caller identities", config.Name)
		}
		callers := map[string]bool{}
		for _, rawID := range config.AllowedSPIFFEIDs {
			id, err := spiffeid.FromString(rawID)
			if err != nil || !listenerIDs[id.String()] {
				return nil, fmt.Errorf("GitHub grant %s caller must be allowed by the SPIFFE listener", config.Name)
			}
			callers[id.String()] = true
		}
		minter, err := githubapp.New(appID, installationID, key, config.RepositoryIDs, config.Profile)
		if err != nil {
			return nil, fmt.Errorf("GitHub grant %s: %w", config.Name, err)
		}
		grants[config.Name] = githubGrant{minter: minter, callers: callers, profile: config.Profile}
	}
	return grants, nil
}

// GitHub grants deliberately have their own route and in-memory cache. They
// cannot be fetched through the legacy OAuth endpoint or its plaintext mode.
// The identity here is a trusted SERVICE caller, not a guest-supplied role.
func (s *server) githubHandler(mtlsListener bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/github/grants/"), "/")
		if len(parts) != 2 || parts[1] != "token" || r.Method != http.MethodGet || r.URL.RawQuery != "" {
			http.NotFound(w, r)
			return
		}
		grant, ok := s.githubGrants[parts[0]]
		if !ok {
			http.NotFound(w, r)
			return
		}
		caller := ""
		if mtlsListener && r.TLS != nil && len(r.TLS.PeerCertificates) > 0 {
			if id, err := x509svid.IDFromCert(r.TLS.PeerCertificates[0]); err == nil {
				caller = id.String()
			}
		}
		if caller == "" || !grant.callers[caller] {
			s.logger.Warn("GitHub grant denied", "grant", parts[0], "spiffe_id", caller)
			writeJSON(w, http.StatusForbidden, map[string]string{"reason": "github_grant_not_authorized"})
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
		defer cancel()
		token, err := grant.minter.Mint(ctx)
		if err != nil {
			s.logger.Warn("GitHub grant mint failed", "grant", parts[0], "spiffe_id", caller, "err", err)
			writeJSON(w, http.StatusBadGateway, map[string]string{"reason": "github_token_mint_failed"})
			return
		}
		s.logger.Info("GitHub grant served", "grant", parts[0], "profile", grant.profile, "spiffe_id", caller)
		writeJSON(w, http.StatusOK, map[string]any{"access_token": token.AccessToken, "expires_at": token.ExpiresAt})
	}
}
