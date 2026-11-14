// Package fakes holds test doubles for the broker's external services: the
// OpenAI device-auth and token endpoints, Substrate's actor JWT issuer, and
// helpers for Codex-shaped tokens.
package fakes

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// AccessToken builds an unsigned Codex-shaped access token carrying exp and
// the ChatGPT account claim the relay reads.
func AccessToken(account string, exp time.Time, nonce string) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	payload, _ := json.Marshal(map[string]any{
		"exp": exp.Unix(), "nonce": nonce,
		"https://api.openai.com/auth": map[string]string{"chatgpt_account_id": account},
	})
	return header + "." + base64.RawURLEncoding.EncodeToString(payload) + ".sig"
}

// OpenAIAuth fakes auth.openai.com's device authorization and token
// endpoints with rotating refresh tokens, reuse detection and revocation.
type OpenAIAuth struct {
	*httptest.Server
	AccountID string
	TokenTTL  time.Duration

	mu           sync.Mutex
	delay        time.Duration
	current      string
	used         map[string]bool
	revoked      bool
	transient    bool
	serial       int
	refreshCalls int
	beforeReuse  func(token string)
}

func NewOpenAIAuth(t *testing.T) *OpenAIAuth {
	t.Helper()
	f := &OpenAIAuth{AccountID: "acct-1", TokenTTL: 10 * 24 * time.Hour, used: map[string]bool{}}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/accounts/deviceauth/usercode", func(w http.ResponseWriter, r *http.Request) {
		// The live endpoint sends numbers as strings.
		writeJSON(w, http.StatusOK, map[string]string{"device_auth_id": "dev-1", "user_code": "ABCD-1234", "interval": "1", "expires_in": "900"})
	})
	mux.HandleFunc("POST /api/accounts/deviceauth/token", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"authorization_code": "auth-code", "code_verifier": "verifier", "code_challenge": "challenge"})
	})
	mux.HandleFunc("POST /oauth/token", f.token)
	f.Server = httptest.NewServer(mux)
	t.Cleanup(f.Close)
	return f
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (f *OpenAIAuth) token(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_request"})
		return
	}
	switch r.PostForm.Get("grant_type") {
	case "authorization_code":
		if r.PostForm.Get("code") != "auth-code" || r.PostForm.Get("code_verifier") != "verifier" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
			return
		}
		f.mu.Lock()
		resp := f.issueLocked()
		f.mu.Unlock()
		writeJSON(w, http.StatusOK, resp)
	case "refresh_token":
		f.mu.Lock()
		f.refreshCalls++
		delay := f.delay
		f.mu.Unlock()
		time.Sleep(delay)
		rt := r.PostForm.Get("refresh_token")
		f.mu.Lock()
		switch {
		case f.transient:
			f.mu.Unlock()
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "server_error"})
		case f.revoked:
			f.mu.Unlock()
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
		case rt != "" && rt == f.current:
			f.used[rt] = true
			resp := f.issueLocked()
			f.mu.Unlock()
			writeJSON(w, http.StatusOK, resp)
		case f.used[rt]:
			hook := f.beforeReuse
			f.mu.Unlock()
			if hook != nil {
				hook(rt)
			}
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "refresh_token_reused"})
		default:
			f.mu.Unlock()
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid_grant"})
		}
	default:
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "unsupported_grant_type"})
	}
}

func (f *OpenAIAuth) issueLocked() map[string]any {
	f.serial++
	f.current = fmt.Sprintf("rt-%d", f.serial)
	return map[string]any{
		"id_token":      "id-token",
		"access_token":  AccessToken(f.AccountID, time.Now().Add(f.TokenTTL), fmt.Sprint(f.serial)),
		"refresh_token": f.current,
		"expires_in":    int(f.TokenTTL.Seconds()),
	}
}

// Issue mints a grant out of band, as a completed login would, and returns
// its access and refresh tokens.
func (f *OpenAIAuth) Issue() (access, refresh string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	resp := f.issueLocked()
	return resp["access_token"].(string), resp["refresh_token"].(string)
}

// Rotate consumes the current refresh token as another writer would and
// returns the new one.
func (f *OpenAIAuth) Rotate() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.used[f.current] = true
	return f.issueLocked()["refresh_token"].(string)
}

func (f *OpenAIAuth) Revoke() {
	f.mu.Lock()
	f.revoked = true
	f.mu.Unlock()
}

// FailTransiently makes refreshes answer 500 server_error, which is not a
// dead grant.
func (f *OpenAIAuth) FailTransiently(fail bool) {
	f.mu.Lock()
	f.transient = fail
	f.mu.Unlock()
}

func (f *OpenAIAuth) SetDelay(d time.Duration) {
	f.mu.Lock()
	f.delay = d
	f.mu.Unlock()
}

// BeforeReuse runs when a used refresh token is presented, before the
// refresh_token_reused answer.
func (f *OpenAIAuth) BeforeReuse(hook func(token string)) {
	f.mu.Lock()
	f.beforeReuse = hook
	f.mu.Unlock()
}

func (f *OpenAIAuth) RefreshCalls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.refreshCalls
}
