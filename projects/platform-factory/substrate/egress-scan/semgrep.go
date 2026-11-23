package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

// The second scan tier sends the text that Gitleaks scans to a Semgrep engine
// and merges the two verdicts. It is off unless the operator enables it.

const (
	failureDeny  = "deny"
	failureAllow = "allow"

	// The engine child reads at most 16 MiB. The scanner sends at most
	// maxBody + maxAddressAndHeaders + the decoded JSON text.
	maxEngineRequest = 16 << 20

	maxLoggedFindings = 5
	logInterval       = 10 * time.Second
)

// Tier outcomes. The first two are verdicts. The next five apply the failure
// mode. The last one means that Gitleaks or the caller ended the call first.
const (
	engineFound       = "found"
	engineClean       = "clean"
	engineTimeout     = "timeout"
	engineError       = "error"
	engineShed        = "shed"
	engineBreakerOpen = "breaker_open"
	engineOversize    = "oversize"
	engineCancelled   = "cancelled"
)

var engineOutcomes = []string{
	engineFound, engineClean, engineTimeout, engineError,
	engineShed, engineBreakerOpen, engineOversize, engineCancelled,
}

// engineUnavailableReason is the deny body when the failure mode is deny.
// It is not a credential finding, so it has its own metric outcome.
const engineUnavailableReason = "secondary scan unavailable"

var (
	errEngineStatus     = errors.New("engine returned an unexpected status")
	errEngineTooLarge   = errors.New("engine response exceeds the size limit")
	errEngineGarbage    = errors.New("engine response is not a scan result")
	errEngineIncomplete = errors.New("engine reported a scan error")
	errEnginePanic      = errors.New("engine panicked")
)

// Finding is one engine result. It holds the rule id and the position in the
// text sent to the engine. It never holds the matched text.
type Finding struct {
	RuleID string
	Start  int
	End    int
}

// Verdict is the engine answer. No findings means the text is clean.
type Verdict struct {
	Findings []Finding
}

// Engine scans one body. An error means that no verdict exists. The call must
// return when ctx ends. Implementations must not log or return the body.
type Engine interface {
	Scan(ctx context.Context, body []byte) (Verdict, error)
}

// semgrepConfig is the operator configuration of the tier.
type semgrepConfig struct {
	Endpoint        string
	Deadline        time.Duration
	FailureMode     string
	MaxBody         int
	Concurrency     int
	MaxResponse     int64
	BreakerFailures int
	BreakerCooldown time.Duration
}

func (c semgrepConfig) validate() error {
	if c.Deadline < 10*time.Millisecond || c.Deadline > 10*time.Second {
		return errors.New("semgrep deadline must be between 10ms and 10s")
	}
	if c.FailureMode != failureDeny && c.FailureMode != failureAllow {
		return errors.New("semgrep failure mode must be deny or allow")
	}
	if c.MaxBody < 1 || c.MaxBody > maxBody {
		return fmt.Errorf("semgrep max body must be between 1 and %d bytes", maxBody)
	}
	if c.Concurrency < 1 || c.Concurrency > 1024 {
		return errors.New("semgrep concurrency must be between 1 and 1024")
	}
	if c.MaxResponse < 1<<10 || c.MaxResponse > maxEngineRequest {
		return errors.New("semgrep max response must be between 1 KiB and 16 MiB")
	}
	if c.BreakerFailures < 0 || c.BreakerFailures > 1000 {
		return errors.New("semgrep breaker failures must be between 0 and 1000")
	}
	if c.BreakerFailures > 0 && (c.BreakerCooldown < 100*time.Millisecond || c.BreakerCooldown > time.Hour) {
		return errors.New("semgrep breaker cooldown must be between 100ms and 1h")
	}
	return nil
}

// httpEngine posts the body to one configured URL. The URL must be reached over
// the cluster network. The client adds no TLS or credentials (see README).
type httpEngine struct {
	url         string
	client      *http.Client
	maxResponse int64
}

func newHTTPEngine(endpoint string, maxResponse int64) (*httpEngine, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || u.Fragment != "" {
		return nil, errors.New("semgrep endpoint must be an http or https URL without credentials")
	}
	transport := &http.Transport{
		// A cluster endpoint needs no proxy, and a proxy could see the body.
		Proxy: nil,
		// A forked engine child exits after one request.
		DisableKeepAlives:      true,
		DisableCompression:     true,
		MaxResponseHeaderBytes: 16 << 10,
	}
	return &httpEngine{
		url: u.String(),
		client: &http.Client{
			Transport: transport,
			// A redirect could send the body to another host.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
		maxResponse: maxResponse,
	}, nil
}

func (e *httpEngine) Scan(ctx context.Context, body []byte) (Verdict, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.url, bytes.NewReader(body))
	if err != nil {
		return Verdict{}, err
	}
	req.Header.Set("Content-Type", "text/plain; charset=utf-8")
	req.Header.Set("User-Agent", "egress-scan")
	resp, err := e.client.Do(req)
	if err != nil {
		return Verdict{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return Verdict{}, errEngineStatus
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, e.maxResponse+1))
	if err != nil {
		return Verdict{}, err
	}
	if int64(len(data)) > e.maxResponse {
		return Verdict{}, errEngineTooLarge
	}
	return parseSemgrepJSON(data)
}

// semgrepOutput lists the fields that the tier reads. It leaves out
// extra.lines and the other fields that can hold the matched text.
type semgrepOutput struct {
	Results *[]struct {
		CheckID string `json:"check_id"`
		Start   struct {
			Offset int `json:"offset"`
		} `json:"start"`
		End struct {
			Offset int `json:"offset"`
		} `json:"end"`
	} `json:"results"`
	// The scanner counts errors and skipped paths. It does not read their
	// content, which can name files and rules.
	Errors []json.RawMessage `json:"errors"`
	Paths  struct {
		Skipped []json.RawMessage `json:"skipped"`
	} `json:"paths"`
}

var ruleIDPattern = regexp.MustCompile(`^[A-Za-z0-9._:/-]{1,160}$`)

// parseSemgrepJSON reads the engine output. The engine writes a "." progress
// line before the JSON, so parsing starts at the first "{". Only dots and
// white space may come before it. Other text is no scan result.
//
// Mapping: each result is a finding, whatever its severity or validation
// state, and also when "is_ignored" is true. The scanned text is actor
// controlled, so an actor can add a "nosemgrep" marker; a suppressed result
// must still deny. The tier does not weigh a result by its secret validation:
// an invalid or unvalidated credential still must not leave. A document with
// no "results" key is garbage. With no finding, any "errors" entry (at any
// level: the CLI reports rule timeouts and out-of-memory at "warn") or any
// skipped path (for example text over the default target size limit) means a
// partial scan, so the verdict is unknown.
func parseSemgrepJSON(data []byte) (Verdict, error) {
	start := bytes.IndexByte(data, '{')
	if start < 0 || len(bytes.Trim(data[:start], ".\r\n\t ")) > 0 {
		return Verdict{}, errEngineGarbage
	}
	var out semgrepOutput
	if err := json.NewDecoder(bytes.NewReader(data[start:])).Decode(&out); err != nil || out.Results == nil {
		return Verdict{}, errEngineGarbage
	}
	var verdict Verdict
	for _, r := range *out.Results {
		id := r.CheckID
		if !ruleIDPattern.MatchString(id) {
			id = "invalid-rule-id"
		}
		verdict.Findings = append(verdict.Findings, Finding{RuleID: id, Start: max(r.Start.Offset, 0), End: max(r.End.Offset, 0)})
	}
	if len(verdict.Findings) == 0 && (len(out.Errors) > 0 || len(out.Paths.Skipped) > 0) {
		return Verdict{}, errEngineIncomplete
	}
	return verdict, nil
}

// breaker stops calls to a failing engine, so that each request does not wait
// for the full deadline. After the cooldown it lets one probe call through.
type breaker struct {
	mu        sync.Mutex
	threshold int
	cooldown  time.Duration
	now       func() time.Time
	failures  int
	openUntil time.Time
	probing   bool
}

func (b *breaker) allow() bool {
	if b.threshold <= 0 {
		return true
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.openUntil.IsZero() {
		return true
	}
	if b.now().Before(b.openUntil) || b.probing {
		return false
	}
	b.probing = true
	return true
}

func (b *breaker) success() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failures, b.openUntil, b.probing = 0, time.Time{}, false
}

func (b *breaker) failure() {
	if b.threshold <= 0 {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failures++
	b.probing = false
	if b.failures >= b.threshold {
		b.openUntil = b.now().Add(b.cooldown)
	}
}

// abandon releases a probe whose call ended without a verdict or a failure.
func (b *breaker) abandon() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.probing = false
}

func (b *breaker) open() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return !b.openUntil.IsZero() && (b.probing || b.now().Before(b.openUntil))
}

// engineResult is the outcome of one tier call.
type engineResult struct {
	outcome string
	verdict Verdict
	err     error
}

func (r engineResult) failed() bool {
	switch r.outcome {
	case engineTimeout, engineError, engineShed, engineBreakerOpen, engineOversize:
		return true
	}
	return false
}

// semgrepTier runs the engine under its own deadline, in-flight limit and
// breaker. The tier never changes the Gitleaks path.
type semgrepTier struct {
	engine  Engine
	cfg     semgrepConfig
	slots   chan struct{}
	breaker *breaker
	metrics *tierMetrics

	logMu   sync.Mutex
	lastLog map[string]time.Time
	now     func() time.Time
}

func newSemgrepTier(cfg semgrepConfig, engine Engine) (*semgrepTier, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	t := &semgrepTier{
		engine:  engine,
		cfg:     cfg,
		slots:   make(chan struct{}, cfg.Concurrency),
		now:     time.Now,
		lastLog: map[string]time.Time{},
	}
	t.breaker = &breaker{threshold: cfg.BreakerFailures, cooldown: cfg.BreakerCooldown, now: time.Now}
	t.metrics = newTierMetrics(t)
	return t, nil
}

// start runs one engine call in its own goroutine and returns its result
// channel. ctx ends the call early. The channel holds one buffered result, so
// the goroutine never blocks if the caller stops reading.
func (t *semgrepTier) start(ctx context.Context, payload []byte, bodyLen int) <-chan engineResult {
	done := make(chan engineResult, 1)
	go func() {
		// run recovers an Engine panic. This is the last guard: a panic in a
		// goroutine would end the whole scanner and deny all egress.
		defer func() {
			if recover() != nil {
				done <- engineResult{outcome: engineError, err: errEnginePanic}
			}
		}()
		done <- t.run(ctx, payload, bodyLen)
	}()
	return done
}

// scan calls the engine and turns a panic into an error. The panic value is
// dropped: it can hold part of the body.
func (t *semgrepTier) scan(ctx context.Context, payload []byte) (verdict Verdict, err error) {
	defer func() {
		if recover() != nil {
			verdict, err = Verdict{}, errEnginePanic
		}
	}()
	return t.engine.Scan(ctx, payload)
}

func (t *semgrepTier) run(ctx context.Context, payload []byte, bodyLen int) engineResult {
	began := time.Now()
	res := t.call(ctx, payload, bodyLen)
	t.metrics.observe(res.outcome, time.Since(began))
	return res
}

func (t *semgrepTier) call(ctx context.Context, payload []byte, bodyLen int) engineResult {
	if bodyLen > t.cfg.MaxBody || len(payload) > maxEngineRequest {
		return engineResult{outcome: engineOversize}
	}
	select {
	case t.slots <- struct{}{}:
		defer func() { <-t.slots }()
	default:
		return engineResult{outcome: engineShed}
	}
	if !t.breaker.allow() {
		return engineResult{outcome: engineBreakerOpen}
	}
	t.metrics.inflight.Inc()
	defer t.metrics.inflight.Dec()

	callCtx, cancel := context.WithTimeout(ctx, t.cfg.Deadline)
	defer cancel()
	verdict, err := t.scan(callCtx, payload)
	switch {
	case err == nil:
		t.breaker.success()
		if len(verdict.Findings) > 0 {
			return engineResult{outcome: engineFound, verdict: verdict}
		}
		return engineResult{outcome: engineClean}
	case ctx.Err() != nil:
		// Gitleaks decided, or the caller gave up. This is no engine fault.
		t.breaker.abandon()
		return engineResult{outcome: engineCancelled, err: ctx.Err()}
	case errors.Is(callCtx.Err(), context.DeadlineExceeded):
		t.breaker.failure()
		return engineResult{outcome: engineTimeout, err: context.DeadlineExceeded}
	default:
		t.breaker.failure()
		return engineResult{outcome: engineError, err: err}
	}
}

// decide turns a finished tier call into a verdict for the request. It returns
// a deny response or nil to allow. It counts the failure mode that it applies.
func (t *semgrepTier) decide(res engineResult) *denial {
	switch {
	case res.outcome == engineFound:
		t.logFound(res.verdict)
		return &denial{reason: "request contains a credential pattern", found: true}
	case res.failed():
		t.metrics.failureMode.WithLabelValues(t.cfg.FailureMode).Inc()
		t.logFailure(res)
		if t.cfg.FailureMode == failureDeny {
			return &denial{reason: engineUnavailableReason}
		}
	}
	return nil
}

type denial struct {
	reason string
	found  bool
}

// logFound writes rule ids and positions. It never writes the matched text.
func (t *semgrepTier) logFound(v Verdict) {
	parts := make([]string, 0, maxLoggedFindings)
	for i, f := range v.Findings {
		if i == maxLoggedFindings {
			break
		}
		parts = append(parts, fmt.Sprintf("%s@%d-%d", f.RuleID, f.Start, f.End))
	}
	log.Printf("semgrep tier blocked a request: %d finding(s): %s", len(v.Findings), strings.Join(parts, " "))
}

// logFailure writes one line per outcome in each log interval. A failing
// engine would otherwise write one line per request.
func (t *semgrepTier) logFailure(res engineResult) {
	t.logMu.Lock()
	now := t.now()
	if last, ok := t.lastLog[res.outcome]; ok && now.Sub(last) < logInterval {
		t.logMu.Unlock()
		return
	}
	t.lastLog[res.outcome] = now
	t.logMu.Unlock()
	detail := ""
	if res.err != nil && res.outcome == engineError {
		detail = ": " + errorKind(res.err)
	}
	log.Printf("semgrep tier %s, failure mode %s%s", res.outcome, t.cfg.FailureMode, detail)
}

// errorKind names an engine error without text that came from the body or
// from the response.
func errorKind(err error) string {
	switch {
	case errors.Is(err, errEngineStatus), errors.Is(err, errEngineTooLarge), errors.Is(err, errEngineGarbage), errors.Is(err, errEngineIncomplete), errors.Is(err, errEnginePanic):
		return err.Error()
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return "request failed"
	}
	return "read failed"
}

// engineText builds the text sent to the engine. It holds the same fields that
// Gitleaks scans: the address, query and headers, the body, and the decoded
// JSON strings.
func engineText(fields string, body []byte, decoded string) []byte {
	out := make([]byte, 0, len(fields)+len(body)+len(decoded)+2)
	out = append(out, fields...)
	out = append(out, '\n')
	out = append(out, body...)
	if decoded != "" {
		out = append(out, '\n')
		out = append(out, decoded...)
	}
	return out
}
