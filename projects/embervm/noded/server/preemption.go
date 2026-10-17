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

// Retry pacing includes startup failures. The cap limits the delay before
// another attempt without spinning during a sustained outage.
const (
	preemptionRetryBase = 2 * time.Second
	preemptionRetryMax  = 30 * time.Second
)

const gceMetadataWaitTimeout = 360

// WatchPreemptionNotices watches GCE's preempted metadata value with hanging
// GETs. When enabled, transient failures (including startup 404s) retry until
// cancellation. Non-GCP nodes should leave the optional watcher disabled.
//
// endpoint and client are injectable so tests never contact the real metadata
// server. cancel enters the same context-driven shutdown path used by SIGTERM.
func (s *Server) WatchPreemptionNotices(ctx context.Context, client *http.Client, endpoint string, cancel context.CancelFunc) {
	s.watchPreemptionNotices(ctx, client, endpoint, cancel, waitPreemptionRetry)
}

func waitPreemptionRetry(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return ctx.Err() == nil
	}
}

func (s *Server) watchPreemptionNotices(ctx context.Context, client *http.Client, endpoint string, cancel context.CancelFunc, wait func(context.Context, time.Duration) bool) {
	if !s.cfg.PreemptionNoticeEnabled {
		return
	}
	if client == nil {
		client = http.DefaultClient
	}

	etag := "0"
	backoff := preemptionRetryBase
	fail := func(msg string, args ...any) bool {
		if ctx.Err() != nil {
			return false
		}
		s.logger.Warn(msg, append(args, "retry_in", backoff)...)
		if !wait(ctx, backoff) {
			return false
		}
		backoff = min(backoff*2, preemptionRetryMax)
		return true
	}
	for {
		requestURL, err := url.Parse(endpoint)
		if err != nil {
			s.logger.Warn("GCE preemption notice watcher: bad endpoint", "err", err)
			return
		}
		if (requestURL.Scheme != "http" && requestURL.Scheme != "https") || requestURL.Host == "" {
			s.logger.Warn("GCE preemption notice watcher: endpoint must be an absolute HTTP URL")
			return
		}
		query := requestURL.Query()
		query.Set("wait_for_change", "true")
		query.Set("last_etag", etag)
		query.Set("timeout_sec", fmt.Sprint(gceMetadataWaitTimeout))
		requestURL.RawQuery = query.Encode()

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, requestURL.String(), nil)
		if err != nil {
			s.logger.Warn("GCE preemption notice watcher: request build failed", "err", err)
			return
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

		nextETag := resp.Header.Get("ETag")
		if nextETag == "" {
			if !fail("GCE preemption notice watcher: metadata response omitted ETag") {
				return
			}
			continue
		}
		backoff = preemptionRetryBase
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
