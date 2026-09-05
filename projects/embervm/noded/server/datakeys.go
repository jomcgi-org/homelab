package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/store"
)

const (
	wrapArtifactPath   = "/v1/artifacts/wrap"
	rewrapArtifactPath = "/v1/artifacts/rewrap"
)

// ErrWrapRejected identifies an HTTP refusal from the control plane's artifact
// wrap endpoint. Transport failures and response decoding failures do not wrap
// this sentinel.
var ErrWrapRejected = errors.New("control plane rejected artifact wrap request")

// WrapRejectedError preserves the HTTP status and safe response reason so an
// export caller can distinguish artifact-specific refusals from retryable
// control-plane and transport failures.
type WrapRejectedError struct {
	StatusCode int
	Reason     string
}

func (e *WrapRejectedError) Error() string {
	return fmt.Sprintf("control plane rejected artifact wrap request: status %d", e.StatusCode)
}

func (e *WrapRejectedError) Unwrap() error { return ErrWrapRejected }

// Permanent reports whether this response identifies an artifact the control
// plane no longer knows. Status alone is not enough: 400 is a daemon request
// bug and every 403 is a global control-plane configuration or identity fault.
func (e *WrapRejectedError) Permanent() bool {
	return e.StatusCode == http.StatusNotFound && e.Reason == "unknown_artifact"
}

type cpDataKeyProvider struct {
	controlPlaneURL string
	tokenPath       string
	doer            httpDoer
}

// NewCPDataKeyProvider creates the dial-home HTTP implementation of
// store.DataKeyProvider. It reads the projected ServiceAccount token fresh for
// every request and applies a 10-second request timeout.
func NewCPDataKeyProvider(controlPlaneURL, tokenPath string) store.DataKeyProvider {
	return newCPDataKeyProvider(controlPlaneURL, tokenPath, &http.Client{Timeout: 10 * time.Second})
}

func newCPDataKeyProvider(controlPlaneURL, tokenPath string, doer httpDoer) *cpDataKeyProvider {
	return &cpDataKeyProvider{
		controlPlaneURL: strings.TrimRight(controlPlaneURL, "/"),
		tokenPath:       tokenPath,
		doer:            doer,
	}
}

func (p *cpDataKeyProvider) DataKey(ctx context.Context, kind, workload, ref string) ([]byte, []byte, error) {
	body, err := json.Marshal(struct {
		Kind     string `json:"kind"`
		Workload string `json:"workload"`
		Ref      string `json:"ref"`
	}{Kind: kind, Workload: workload, Ref: ref})
	if err != nil {
		return nil, nil, fmt.Errorf("marshal artifact wrap request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.controlPlaneURL+wrapArtifactPath, bytes.NewReader(body))
	if err != nil {
		return nil, nil, fmt.Errorf("build artifact wrap request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token := readControlPlaneToken(p.tokenPath); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := p.doer.Do(req)
	if err != nil {
		return nil, nil, fmt.Errorf("post artifact wrap request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		reason := responseErrorReason(resp.Body)
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
		return nil, nil, &WrapRejectedError{
			StatusCode: resp.StatusCode,
			Reason:     reason,
		}
	}
	var out struct {
		DataKey  []byte `json:"data_key"`
		Envelope []byte `json:"envelope"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil {
		return nil, nil, fmt.Errorf("decode artifact wrap response: %w", err)
	}
	if len(out.DataKey) != 32 {
		return nil, nil, fmt.Errorf("artifact wrap response data_key length %d, want 32", len(out.DataKey))
	}
	if len(out.Envelope) == 0 {
		return nil, nil, fmt.Errorf("artifact wrap response envelope is empty")
	}
	return out.DataKey, out.Envelope, nil
}

func responseErrorReason(body io.Reader) string {
	var out struct {
		Error string `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(body, 4<<10)).Decode(&out); err != nil {
		return ""
	}
	return out.Error
}

// RewrapEnvelope asks the control plane to move one opaque artifact envelope
// to its current root generation. The data key never enters this response.
func (p *cpDataKeyProvider) RewrapEnvelope(ctx context.Context, kind, workload, ref string, envelope []byte) ([]byte, bool, error) {
	body, err := json.Marshal(struct {
		Kind     string `json:"kind"`
		Workload string `json:"workload"`
		Ref      string `json:"ref"`
		Envelope []byte `json:"envelope"`
	}{Kind: kind, Workload: workload, Ref: ref, Envelope: envelope})
	if err != nil {
		return nil, false, fmt.Errorf("marshal artifact rewrap request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.controlPlaneURL+rewrapArtifactPath, bytes.NewReader(body))
	if err != nil {
		return nil, false, fmt.Errorf("build artifact rewrap request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token := readControlPlaneToken(p.tokenPath); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := p.doer.Do(req)
	if err != nil {
		return nil, false, fmt.Errorf("post artifact rewrap request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
		return nil, false, fmt.Errorf("control plane rejected artifact rewrap request: status %d", resp.StatusCode)
	}
	var out struct {
		Changed  bool   `json:"changed"`
		Envelope []byte `json:"envelope"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil {
		return nil, false, fmt.Errorf("decode artifact rewrap response: %w", err)
	}
	if len(out.Envelope) == 0 {
		return nil, false, fmt.Errorf("artifact rewrap response envelope is empty")
	}
	return out.Envelope, out.Changed, nil
}
