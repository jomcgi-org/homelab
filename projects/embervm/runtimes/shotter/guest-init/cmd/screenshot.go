package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

const (
	minDimension  = 1
	maxDimension  = 4096
	defaultWidth  = 1920
	defaultHeight = 1080

	// maxEncodedPNGBytes is the ceiling on the BASE64-ENCODED PNG, checked
	// against the encoded byte count, not the decoded one. An oversized
	// capture is refused, never truncated, matching what the Phase 3
	// monolith specs assert.
	//
	// Sizes nest the same way the timeouts do, and this is the innermost
	// one. The workload's invocation.resultMaxBytes is 8 MiB (see
	// chart/templates/workload-shotter.yaml), which itself sits under the
	// 32 MiB noded reads back from a guest. What crosses that transport is
	// this handler's JSON envelope with the PNG base64-encoded inside it,
	// never the raw decoded PNG, so the cap here has to leave real headroom
	// under resultMaxBytes for the envelope (the other scalar fields, field
	// names, quoting) on top of the encoded payload itself. 7 MiB leaves
	// 1 MiB of that headroom. Staying under it is what makes an oversized
	// capture a legible error from this handler rather than a silent
	// dispatcher-side truncation or a JSON decode error several hops away,
	// where the caller cannot tell what went wrong. Raising this without
	// raising resultMaxBytes first, or without reasoning about the encoded
	// size, just moves the failure somewhere less informative: 6 MiB decoded
	// is exactly 8 MiB base64-encoded, with zero room for the envelope.
	maxEncodedPNGBytes = 7 << 20

	defaultWaitUntil = "load"

	minNavigateTimeoutMs     = 1_000
	defaultNavigateTimeoutMs = 20_000
	maxNavigateTimeoutMs     = 45_000

	// handlerOverhead and maxHandlerCap bound this handler's own work
	// (target create, websocket dial, screenshot encode, target close)
	// outside of the CDP navigate wait it wraps. ADR embervm/035 section 5
	// nests timeouts strictly: Context Forge 60s, then the monolith client's
	// 55s read timeout, then the workload's 50s timeoutSeconds, then this
	// handler, then the CDP navigate timeout innermost. Deriving the handler
	// cap from the request's own navigate timeout, and then clamping it to
	// an absolute ceiling independent of what the caller asked for, keeps
	// this layer inside whatever budget the layer above it granted even if a
	// caller requests the maximum navigate timeout: maxHandlerCap must stay
	// strictly under the workload's 50s, not merely under the monolith
	// client's 55s, or a slow-but-within-budget handler would blow the
	// workload's own deadline before ever getting a chance to answer.
	handlerOverhead = 10 * time.Second
	maxHandlerCap   = 48 * time.Second

	// maxRequestBodyBytes is generous for a handful of scalar fields; it
	// exists to refuse a malformed or hostile body before it reaches the
	// JSON decoder.
	maxRequestBodyBytes = 64 << 10

	screenshotContentType = "application/json"
)

var (
	errValidation        = errors.New("invalid screenshot request")
	errNavigationFailed  = errors.New("navigation failed")
	errNavigationTimeout = errors.New("navigation timed out")
	errCaptureTooLarge   = errors.New("screenshot capture exceeds size limit")
)

// waitUntilEvents maps the request's wait_until value to the CDP lifecycle
// event this handler waits for. Only two values are supported: a
// network-idle heuristic was considered and rejected for this first version
// because it cannot be verified in a unit test and would need Network
// domain bookkeeping this handler does not otherwise carry.
var waitUntilEvents = map[string]string{
	"load":             "Page.loadEventFired",
	"domcontentloaded": "Page.domContentEventFired",
}

func supportedWaitUntilValues() []string {
	values := make([]string, 0, len(waitUntilEvents))
	for value := range waitUntilEvents {
		values = append(values, value)
	}
	sort.Strings(values)
	return values
}

// screenshotRequest is the wire shape of a POST /shim/screenshot body.
type screenshotRequest struct {
	URL       string `json:"url"`
	Width     int    `json:"width"`
	Height    int    `json:"height"`
	FullPage  bool   `json:"full_page"`
	WaitUntil string `json:"wait_until"`
	TimeoutMs int    `json:"timeout_ms"`
}

// screenshotResponse is pinned by the Phase 3 monolith specs; the field
// names and shape here must not drift from {png_b64, width, height,
// final_url, status, duration_ms}.
type screenshotResponse struct {
	PNGBase64  string `json:"png_b64"`
	Width      int    `json:"width"`
	Height     int    `json:"height"`
	FinalURL   string `json:"final_url"`
	Status     int    `json:"status"`
	DurationMs int64  `json:"duration_ms"`
}

// validatedScreenshot is a screenshotRequest that has passed every check:
// bounded dimensions, a supported wait_until, a bounded timeout, and a
// navigate URL already downgraded to plaintext http.
type validatedScreenshot struct {
	navigateURL     string
	width, height   int
	fullPage        bool
	waitUntilEvent  string
	navigateTimeout time.Duration
}

func parseScreenshotRequest(body io.Reader) (screenshotRequest, error) {
	decoder := json.NewDecoder(io.LimitReader(body, maxRequestBodyBytes+1))
	decoder.DisallowUnknownFields()
	var req screenshotRequest
	if err := decoder.Decode(&req); err != nil {
		return screenshotRequest{}, fmt.Errorf("%w: decode request body: %v", errValidation, err)
	}
	return req, nil
}

func validateScreenshotRequest(req screenshotRequest) (validatedScreenshot, error) {
	if strings.TrimSpace(req.URL) == "" {
		return validatedScreenshot{}, fmt.Errorf("%w: url is required", errValidation)
	}
	parsed, err := url.Parse(req.URL)
	if err != nil {
		return validatedScreenshot{}, fmt.Errorf("%w: url does not parse: %v", errValidation, err)
	}
	if parsed.Host == "" {
		return validatedScreenshot{}, fmt.Errorf("%w: url must be absolute with an explicit host", errValidation)
	}
	if err := rewriteToPlaintextHTTP(parsed); err != nil {
		return validatedScreenshot{}, err
	}

	width := req.Width
	if width == 0 {
		width = defaultWidth
	}
	if width < minDimension || width > maxDimension {
		return validatedScreenshot{}, fmt.Errorf("%w: width %d is out of bounds [%d, %d]", errValidation, width, minDimension, maxDimension)
	}

	height := req.Height
	if height == 0 {
		height = defaultHeight
	}
	if height < minDimension || height > maxDimension {
		return validatedScreenshot{}, fmt.Errorf("%w: height %d is out of bounds [%d, %d]", errValidation, height, minDimension, maxDimension)
	}

	waitUntil := req.WaitUntil
	if waitUntil == "" {
		waitUntil = defaultWaitUntil
	}
	waitEvent, ok := waitUntilEvents[waitUntil]
	if !ok {
		return validatedScreenshot{}, fmt.Errorf("%w: wait_until %q must be one of %v", errValidation, waitUntil, supportedWaitUntilValues())
	}

	timeoutMs := req.TimeoutMs
	if timeoutMs == 0 {
		timeoutMs = defaultNavigateTimeoutMs
	}
	if timeoutMs < minNavigateTimeoutMs || timeoutMs > maxNavigateTimeoutMs {
		return validatedScreenshot{}, fmt.Errorf("%w: timeout_ms %d is out of bounds [%d, %d]", errValidation, timeoutMs, minNavigateTimeoutMs, maxNavigateTimeoutMs)
	}

	return validatedScreenshot{
		navigateURL:     parsed.String(),
		width:           width,
		height:          height,
		fullPage:        req.FullPage,
		waitUntilEvent:  waitEvent,
		navigateTimeout: time.Duration(timeoutMs) * time.Millisecond,
	}, nil
}

// rewriteToPlaintextHTTP downgrades an https URL to http in place before
// Chromium ever sees it. This looks wrong at a glance, so the reason is
// recorded here rather than only in the PR: the in-guest proxy (proxy.go)
// maps a mapped public host to its internal destination by host alone and
// always replaces the port with the internal plaintext port 3000 (ADR
// embervm/035 section 3). An https URL makes Chromium issue a CONNECT for
// the requested port, the proxy still maps it to the plaintext 3000
// destination, the allowlist admits it because the destination itself is
// allowlisted, and only then does Chromium's TLS handshake fail against a
// plaintext server. Downgrading here refuses an unroutable request up front
// instead of failing deep inside a TLS handshake with a confusing error.
// This is safe because the proxy replays the request head byte for byte, so
// the origin still sees Host: <the original host> and host-keyed routes
// keep resolving correctly, and because the plaintext hop never leaves the
// cluster: it is a vsock tunnel to an in-cluster service, not a public wire.
func rewriteToPlaintextHTTP(u *url.URL) error {
	switch strings.ToLower(u.Scheme) {
	case "http":
		return nil
	case "https":
		u.Scheme = "http"
		return nil
	default:
		return fmt.Errorf("%w: url scheme %q must be http or https", errValidation, u.Scheme)
	}
}

func handlerCap(navigateTimeout time.Duration) time.Duration {
	total := navigateTimeout + handlerOverhead
	if total > maxHandlerCap {
		return maxHandlerCap
	}
	return total
}

// screenshotHandler drives the already-warm Chromium over CDP and returns a
// PNG. It replaces the T3 stub reserved in main.go.
func screenshotHandler(logger *slog.Logger) http.HandlerFunc {
	cdpHTTPBase := fmt.Sprintf("http://%s:%d", cdpAddress, cdpPort)
	return func(w http.ResponseWriter, r *http.Request) {
		rawReq, err := parseScreenshotRequest(r.Body)
		if err != nil {
			writeScreenshotError(w, logger, err)
			return
		}
		validated, err := validateScreenshotRequest(rawReq)
		if err != nil {
			writeScreenshotError(w, logger, err)
			return
		}

		handlerCtx, cancel := context.WithTimeout(r.Context(), handlerCap(validated.navigateTimeout))
		defer cancel()

		resp, err := captureScreenshotViaCDP(handlerCtx, logger, cdpHTTPBase, validated)
		if err != nil {
			writeScreenshotError(w, logger, err)
			return
		}

		w.Header().Set("Content-Type", screenshotContentType)
		if err := json.NewEncoder(w).Encode(resp); err != nil {
			logger.Error("ember-shotter-init: encode screenshot response failed", "err", err)
		}
	}
}

// writeScreenshotError maps an internal error to the HTTP status a caller
// sees. Every path here is a bounded, legible error rather than a severed
// connection, which is the requirement ADR embervm/035 section 5 places on
// this handler as the innermost layer of the timeout chain.
func writeScreenshotError(w http.ResponseWriter, logger *slog.Logger, err error) {
	switch {
	case errors.Is(err, errValidation):
		http.Error(w, err.Error(), http.StatusBadRequest)
	case errors.Is(err, errCaptureTooLarge):
		http.Error(w, err.Error(), http.StatusRequestEntityTooLarge)
	case errors.Is(err, errNavigationFailed):
		http.Error(w, err.Error(), http.StatusBadGateway)
	case errors.Is(err, errNavigationTimeout), errors.Is(err, context.DeadlineExceeded):
		http.Error(w, err.Error(), http.StatusGatewayTimeout)
	default:
		logger.Error("ember-shotter-init: screenshot handler error", "err", err)
		http.Error(w, "screenshot capture failed", http.StatusInternalServerError)
	}
}
