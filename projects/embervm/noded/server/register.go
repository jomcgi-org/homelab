package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"os"
	"strings"
	"time"
)

// Dial-home registration (R0 PR-2, ADR embervm/005). The daemon ACTIVELY
// advertises its identity to the control plane instead of being discovered:
// the control plane never lists-and-watches noded pods. On start and on a
// jittered interval the daemon POSTs {node, pod_uid, address, boot_id} to
// <ControlPlaneURL>/v1/nodes/register; the control plane upserts the instance
// keyed by (node, pod_uid), dials the advertised address for WatchNode, and
// ages the instance out when both the registration lapses AND the WatchNode
// stream dies.
//
// Registration is ADVERTISEMENT, not liveness: capacity and health still ride
// the WatchNode stream, so a failed POST is retryable and non-fatal, and a
// draining instance simply stops re-advertising (the control plane ages it out
// while its own drain force-banks its live state over the existing per-class
// RPCs). We therefore never crash the daemon on a registration error; we log at
// most once per state change and keep trying.

const registerPath = "/v1/nodes/register"

// registration is the JSON body the daemon POSTs to the control plane. Field
// names are snake_case to match the control plane's decoder.
type registration struct {
	// Node is the Kubernetes node name this daemon is pinned to.
	Node string `json:"node"`
	// PodUID is this pod's Kubernetes UID (Downward API metadata.uid): the
	// INSTANCE identity the control plane keys the registry and ledger by, so two
	// noded instances on one node during a surge roll never alias.
	PodUID string `json:"pod_uid"`
	// Address is "<pod_ip>:<grpc_port>", the endpoint the control plane dials for
	// WatchNode and the Prime/Assign hot path.
	Address string `json:"address"`
	// BootID is a per-process run identity minted once at loop start, so the
	// control plane can tell a fresh pod (new boot_id) from a re-advertisement of
	// the same one, and observe a restart-in-place even when node+pod_uid are
	// unchanged.
	BootID string `json:"boot_id"`
}

// httpDoer is the subset of *http.Client the register loop uses, seamed so a
// test can drive the POST without a real control plane.
type httpDoer interface {
	Do(req *http.Request) (*http.Response, error)
}

// RunRegisterLoop starts the dial-home registration loop and returns
// immediately; the goroutine stops when ctx is cancelled (daemon shutdown).
// It is a no-op (logs a single notice and returns) when no ControlPlaneURL is
// configured, so an out-of-cluster or test daemon never dials home.
//
// Mirrors StartBudgetLoop's shape (start-a-goroutine-return-now) so main.go
// wires it identically. The HTTP client carries a short per-request timeout so a
// wedged control plane never blocks the loop's ticker.
func (s *Server) RunRegisterLoop(ctx context.Context) {
	if strings.TrimSpace(s.cfg.ControlPlaneURL) == "" {
		s.logger.Info("noded: dial-home registration disabled (no EMBERVM_NODED_CONTROL_PLANE_URL)")
		return
	}

	client := &http.Client{Timeout: 5 * time.Second}
	s.runRegisterLoop(ctx, client, newID("boot"), s.cfg.RegisterInterval)
}

// runRegisterLoop is the testable core: an injected doer, a fixed boot id, and
// an explicit interval. It registers once immediately (so the control plane
// adopts a fresh pod without waiting a full interval), then re-registers on a
// jittered tick until ctx is cancelled or the daemon starts draining.
func (s *Server) runRegisterLoop(ctx context.Context, doer httpDoer, bootID string, interval time.Duration) {
	if interval <= 0 {
		interval = 30 * time.Second
	}

	// lastOK tracks the previous attempt's outcome so we log only on a state
	// CHANGE (ok<->fail), never once per successful tick.
	var lastOK bool
	firstAttempt := true

	attempt := func() {
		// A draining instance stops re-advertising: the control plane ages it out
		// while its own drain force-banks the node's live state. Returning here
		// (rather than exiting the loop) keeps the goroutine alive to observe
		// ctx.Done() promptly on shutdown.
		if s.isDraining() {
			return
		}

		err := s.register(ctx, doer, bootID)
		ok := err == nil

		if firstAttempt || ok != lastOK {
			if ok {
				s.logger.Info("noded: dial-home registration ok",
					"controlPlane", s.cfg.ControlPlaneURL, "node", s.cfg.Node, "podUID", s.cfg.PodUID)
			} else {
				s.logger.Warn("noded: dial-home registration failing (will retry)",
					"controlPlane", s.cfg.ControlPlaneURL, "err", err)
			}
		}

		lastOK = ok
		firstAttempt = false
	}

	go func() {
		attempt()
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(jitter(interval)):
				attempt()
			}
		}
	}()
}

// register performs one dial-home POST. It reads the bearer token fresh per
// request (so a rotated projected ServiceAccount token is picked up without a
// restart) and returns a non-nil error on any transport failure or non-2xx
// status, which the loop treats as retryable.
func (s *Server) register(ctx context.Context, doer httpDoer, bootID string) error {
	body, err := json.Marshal(registration{
		Node:    s.cfg.Node,
		PodUID:  s.cfg.PodUID,
		Address: s.advertisedAddress(),
		BootID:  bootID,
	})
	if err != nil {
		return fmt.Errorf("marshal registration: %w", err)
	}

	url := strings.TrimRight(s.cfg.ControlPlaneURL, "/") + registerPath
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build registration request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token := s.controlPlaneToken(); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := doer.Do(req)
	if err != nil {
		return fmt.Errorf("post registration: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("control plane rejected registration: status %d", resp.StatusCode)
	}
	return nil
}

// advertisedAddress is "<pod_ip>:<grpc_port>", the endpoint the control plane
// dials. The port is parsed from ListenAddr (":9090" or "0.0.0.0:9090"); PodIP
// is the routable pod IP. When PodIP is empty (out-of-cluster) we still send the
// port-only form the control plane can dial on localhost in a test.
func (s *Server) advertisedAddress() string {
	port := grpcPortOf(s.cfg.ListenAddr)
	return s.cfg.PodIP + ":" + port
}

// grpcPortOf extracts the port from a "host:port" or ":port" listen address,
// defaulting to 9090 when absent or unparseable.
func grpcPortOf(listenAddr string) string {
	if i := strings.LastIndex(listenAddr, ":"); i >= 0 && i < len(listenAddr)-1 {
		return listenAddr[i+1:]
	}
	return "9090"
}

// controlPlaneToken reads the bearer token file fresh, returning "" (no auth
// header) when the path is unset or unreadable. Fresh-per-request so a rotated
// projected token is picked up without a daemon restart.
func (s *Server) controlPlaneToken() string {
	path := strings.TrimSpace(s.cfg.ControlPlaneTokenPath)
	if path == "" {
		return ""
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

// jitter returns d with +/-10% uniform jitter so a fleet of daemons that booted
// together does not synchronize its re-registration POSTs into a thundering
// herd against the control plane.
func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return d
	}
	delta := int64(d) / 10
	if delta <= 0 {
		return d
	}
	return time.Duration(int64(d) - delta + rand.Int63n(2*delta+1))
}
