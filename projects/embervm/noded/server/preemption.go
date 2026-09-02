package server

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// GCEPreemptionMetadataURL is the instance metadata value Compute Engine flips
// to TRUE when a Spot VM is being preempted.
const GCEPreemptionMetadataURL = "http://metadata.google.internal/computeMetadata/v1/instance/preempted"

// Retry pacing for a metadata server that HAS answered before. Bounded so a
// sustained outage does not spin, and capped well under the interval at which a
// node is plausibly preempted.
const (
	preemptionRetryBase = 2 * time.Second
	preemptionRetryMax  = 30 * time.Second
)

const gceMetadataWaitTimeout = 360

// WatchPreemptionNotices watches GCE's preempted metadata value with hanging
// GETs. It is deliberately fail-soft: any metadata request failure logs once and
// disables the optional watcher, which keeps non-GCP nodes and developer
// machines from retrying a missing metadata server forever.
//
// endpoint and client are injectable so tests never contact the real metadata
// server. cancel enters the same context-driven shutdown path used by SIGTERM.
func (s *Server) WatchPreemptionNotices(ctx context.Context, client *http.Client, endpoint string, cancel context.CancelFunc) {
	if !s.cfg.PreemptionNoticeEnabled {
		return
	}
	if client == nil {
		client = http.DefaultClient
	}

	etag := "0"
	// Distinguishes a metadata server that was NEVER there (a developer machine or
	// a non-GCP cluster: disable quietly, it is not coming) from one that answered
	// and then failed (a blip: retry, because this watcher exists to catch a rare
	// event and a watcher that switched itself off hours ago over a transient error
	// is indistinguishable from one that is working right up until it matters).
	succeeded := false
	backoff := preemptionRetryBase
	fail := func(msg string, args ...any) bool {
		if !succeeded {
			// Never reached the metadata server at all, so this is not GCP. Disable
			// quietly: it is a fact about the environment, not a fault.
			s.logger.Info(msg, args...)
			return false
		}
		// It answered before, so this is a fault rather than an absence. WARN, and
		// keep watching: going quiet here is how a watcher stops covering the event
		// it exists for without anyone noticing.
		s.logger.Warn(msg, append(args, "retry_in", backoff)...)
		select {
		case <-ctx.Done():
			return false
		case <-time.After(backoff):
		}
		if backoff < preemptionRetryMax {
			backoff *= 2
		}
		return true
	}
	for {
		requestURL, err := url.Parse(endpoint)
		if err != nil {
			if !fail("GCE preemption notice watcher: bad endpoint", "err", err) {
				return
			}
			continue
		}
		query := requestURL.Query()
		query.Set("wait_for_change", "true")
		query.Set("last_etag", etag)
		query.Set("timeout_sec", fmt.Sprint(gceMetadataWaitTimeout))
		requestURL.RawQuery = query.Encode()

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL.String(), nil)
		if err != nil {
			if !fail("GCE preemption notice watcher: request build failed", "err", err) {
				return
			}
			continue
		}
		req.Header.Set("Metadata-Flavor", "Google")

		resp, err := client.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if !fail("GCE preemption notice watcher: metadata request failed", "err", err) {
				return
			}
			continue
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 64))
		closeErr := resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			if !fail("GCE preemption notice watcher: metadata returned non-200", "status", resp.StatusCode) {
				return
			}
			continue
		}
		if readErr != nil {
			if !fail("GCE preemption notice watcher: metadata read failed", "err", readErr) {
				return
			}
			continue
		}
		if closeErr != nil {
			if !fail("GCE preemption notice watcher: metadata body close failed", "err", closeErr) {
				return
			}
			continue
		}

		succeeded = true
		backoff = preemptionRetryBase

		nextETag := resp.Header.Get("ETag")
		if nextETag == "" {
			if !fail("GCE preemption notice watcher: metadata response omitted ETag") {
				return
			}
			continue
		}
		etag = nextETag

		if strings.TrimSpace(string(body)) != "TRUE" {
			continue
		}

		deadline := time.Now().Add(s.cfg.PreemptionDrainTimeout)
		effectiveDeadline := s.SetDraining(deadline)
		s.logger.Info("GCE preemption notice received; draining",
			"budget", s.cfg.PreemptionDrainTimeout,
			"deadline", effectiveDeadline.UTC())
		cancel()
		return
	}
}
