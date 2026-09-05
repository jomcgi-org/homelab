package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestParseCodexQuota(t *testing.T) {
	now := time.Date(2026, 9, 5, 18, 10, 0, 0, time.UTC)
	tests := []struct {
		name       string
		headers    http.Header
		statusCode int
		wantOK     bool
		wantStatus string
		wantNames  []string
	}{
		{
			name: "primary only",
			headers: http.Header{
				"X-Codex-Primary-Used-Percent":   []string{"24"},
				"X-Codex-Primary-Window-Minutes": []string{"10080"},
				"X-Codex-Primary-Reset-At":       []string{"1788602828"},
			},
			statusCode: http.StatusOK, wantOK: true, wantStatus: "allowed", wantNames: []string{"primary"},
		},
		{
			name: "both windows",
			headers: http.Header{
				"X-Codex-Primary-Used-Percent":     []string{"24"},
				"X-Codex-Secondary-Used-Percent":   []string{"3.5"},
				"X-Codex-Secondary-Window-Minutes": []string{"300"},
			},
			statusCode: http.StatusOK, wantOK: true, wantStatus: "allowed", wantNames: []string{"primary", "secondary"},
		},
		{
			name:       "429 with reached type",
			headers:    http.Header{"X-Codex-Rate-Limit-Reached-Type": []string{"primary"}},
			statusCode: http.StatusTooManyRequests, wantOK: true, wantStatus: "rejected",
		},
		{name: "no headers", headers: http.Header{}, statusCode: http.StatusOK, wantOK: false},
		{
			name:       "malformed used percent leaves no window and no observation",
			headers:    http.Header{"X-Codex-Primary-Used-Percent": []string{"not-a-number"}},
			statusCode: http.StatusOK, wantOK: false,
		},
		{
			name:       "unrelated x-codex header alone is not an observation",
			headers:    http.Header{"X-Codex-Turn-State": []string{"opaque"}},
			statusCode: http.StatusOK, wantOK: false,
		},
		{
			name:       "429 without windows is still a rejection",
			headers:    http.Header{},
			statusCode: http.StatusTooManyRequests, wantOK: true, wantStatus: "rejected",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			obs, ok := parseCodexQuota(tt.headers, tt.statusCode, now)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v; observation = %#v", ok, tt.wantOK, obs)
			}
			if !ok {
				return
			}
			if obs.Provider != "codex" || obs.Status != tt.wantStatus || obs.ObservedAt != "2026-09-05T18:10:00Z" {
				t.Fatalf("observation = %#v", obs)
			}
			if len(obs.Windows) != len(tt.wantNames) {
				t.Fatalf("windows = %#v, want names %v", obs.Windows, tt.wantNames)
			}
			for i, name := range tt.wantNames {
				if obs.Windows[i].Name != name {
					t.Errorf("window %d name = %q, want %q", i, obs.Windows[i].Name, name)
				}
			}
			if tt.name == "primary only" && obs.Windows[0].ResetsAt != "2026-09-05T10:07:08Z" {
				t.Errorf("reset = %q", obs.Windows[0].ResetsAt)
			}
			if tt.name == "429 with reached type" && obs.ReachedType != "primary" {
				t.Errorf("reached_type = %q", obs.ReachedType)
			}
		})
	}
}

func TestParseClaudeQuota(t *testing.T) {
	now := time.Date(2026, 9, 5, 18, 10, 0, 0, time.UTC)
	tests := []struct {
		name        string
		headers     http.Header
		statusCode  int
		wantOK      bool
		wantStatus  string
		wantUsed    float64
		wantWindows int
	}{
		{
			name: "allowed fraction utilization",
			headers: http.Header{
				"Anthropic-Ratelimit-Unified-Status":         []string{"allowed"},
				"Anthropic-Ratelimit-Unified-5h-Utilization": []string{"0.24"},
				"Anthropic-Ratelimit-Unified-5h-Reset":       []string{"2026-09-05T20:00:00Z"},
			},
			statusCode: http.StatusOK, wantOK: true, wantStatus: "allowed", wantUsed: 24, wantWindows: 1,
		},
		{
			name: "percentage utilization",
			headers: http.Header{
				"Anthropic-Ratelimit-Unified-Status":         []string{"allowed"},
				"Anthropic-Ratelimit-Unified-7d-Utilization": []string{"35.5"},
				"Anthropic-Ratelimit-Unified-7d-Reset":       []string{"1788602828"},
			},
			statusCode: http.StatusOK, wantOK: true, wantStatus: "allowed", wantUsed: 35.5, wantWindows: 1,
		},
		{
			name: "allowed warning",
			headers: http.Header{
				"Anthropic-Ratelimit-Unified-Status":         []string{"allowed_warning"},
				"Anthropic-Ratelimit-Unified-5h-Utilization": []string{"0.91"},
			},
			statusCode: http.StatusOK, wantOK: true, wantStatus: "warning", wantUsed: 91, wantWindows: 1,
		},
		{name: "status only without windows is not an observation", headers: http.Header{"Anthropic-Ratelimit-Unified-Status": []string{"allowed"}}, statusCode: http.StatusOK, wantOK: false},
		{name: "rejected", headers: http.Header{"Anthropic-Ratelimit-Unified-Status": []string{"rejected"}}, statusCode: http.StatusOK, wantOK: true, wantStatus: "rejected"},
		{name: "429 forces rejected", headers: http.Header{}, statusCode: http.StatusTooManyRequests, wantOK: true, wantStatus: "rejected"},
		{name: "no headers", headers: http.Header{}, statusCode: http.StatusOK, wantOK: false},
		{
			name: "malformed values leave no window and no observation",
			headers: http.Header{
				"Anthropic-Ratelimit-Unified-Status":         []string{"allowed"},
				"Anthropic-Ratelimit-Unified-5h-Utilization": []string{"bad"},
			},
			statusCode: http.StatusOK, wantOK: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			obs, ok := parseClaudeQuota(tt.headers, tt.statusCode, now)
			if ok != tt.wantOK {
				t.Fatalf("ok = %v, want %v; observation = %#v", ok, tt.wantOK, obs)
			}
			if !ok {
				return
			}
			if obs.Provider != "claude" || obs.Status != tt.wantStatus {
				t.Fatalf("observation = %#v", obs)
			}
			if len(obs.Windows) != tt.wantWindows {
				t.Fatalf("windows = %#v, want %d", obs.Windows, tt.wantWindows)
			}
			if tt.wantWindows > 0 && obs.Windows[0].UsedPercent != tt.wantUsed {
				t.Errorf("used percent = %v, want %v", obs.Windows[0].UsedPercent, tt.wantUsed)
			}
		})
	}
}

func TestObserveQuotaDispatchesByProvider(t *testing.T) {
	now := time.Date(2026, 9, 5, 18, 10, 0, 0, time.UTC)
	tests := []struct {
		provider string
		header   string
		want     string
	}{
		{provider: "codex", header: "X-Codex-Primary-Used-Percent", want: "codex"},
		{provider: "claude", header: "Anthropic-Ratelimit-Unified-5h-Utilization", want: "claude"},
		{provider: "", want: ""},
	}
	for _, tt := range tests {
		t.Run(tt.provider, func(t *testing.T) {
			header := make(http.Header)
			if tt.header != "" {
				header.Set(tt.header, "0.5")
			}
			obs, ok := observeQuota(&secretEntry{QuotaProvider: tt.provider}, &http.Response{StatusCode: http.StatusOK, Header: header}, now)
			if ok != (tt.want != "") || obs.Provider != tt.want {
				t.Fatalf("observation = %#v, ok = %v", obs, ok)
			}
		})
	}
}

func TestQuotaSummarySelectsHeadlineWindow(t *testing.T) {
	tests := []struct {
		name       string
		obs        QuotaObservation
		wantWindow string
		wantOK     bool
	}{
		{
			name: "codex primary",
			obs: QuotaObservation{Provider: "codex", Windows: []Window{
				{Name: "secondary", UsedPercent: 12},
				{Name: "primary", UsedPercent: 34},
			}},
			wantWindow: "primary", wantOK: true,
		},
		{
			name: "claude 5h",
			obs: QuotaObservation{Provider: "claude", Windows: []Window{
				{Name: "7d", UsedPercent: 56},
				{Name: "5h", UsedPercent: 78},
			}},
			wantWindow: "5h", wantOK: true,
		},
		{
			name: "codex secondary only",
			obs: QuotaObservation{Provider: "codex", Windows: []Window{
				{Name: "secondary", UsedPercent: 12},
			}},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			window, ok := quotaSummary(tt.obs)
			if ok != tt.wantOK || window.Name != tt.wantWindow {
				t.Fatalf("quotaSummary = (%#v, %v), want window %q, ok %v", window, ok, tt.wantWindow, tt.wantOK)
			}
		})
	}
}

func TestQuotaReporterLatestWins(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var mu sync.Mutex
	var received []QuotaObservation
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var obs QuotaObservation
		if err := json.NewDecoder(r.Body).Decode(&obs); err != nil {
			t.Errorf("decode observation: %v", err)
		}
		mu.Lock()
		received = append(received, obs)
		call := len(received)
		mu.Unlock()
		if call == 1 {
			close(started)
			<-release
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	reporter := newQuotaReporter(server.URL, slog.New(slog.NewTextHandler(io.Discard, nil)))
	reporter.report(QuotaObservation{Provider: "codex", ObservedAt: "2026-09-05T18:10:00Z", Status: "allowed"})
	<-started
	reporter.report(QuotaObservation{Provider: "codex", ObservedAt: "2026-09-05T18:11:00Z", Status: "warning"})
	reporter.report(QuotaObservation{Provider: "codex", ObservedAt: "2026-09-05T18:12:00Z", Status: "rejected"})
	close(release)

	deadline := time.Now().Add(time.Second)
	for {
		reporter.pendingMu.Lock()
		done := !reporter.inFlight
		reporter.pendingMu.Unlock()
		if done {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("reporter did not drain")
		}
		time.Sleep(time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(received) != 2 {
		t.Fatalf("POST count = %d, want 2", len(received))
	}
	if received[1].ObservedAt != "2026-09-05T18:12:00Z" {
		t.Fatalf("second POST = %#v, want third observation", received[1])
	}
}

func TestQuotaReporterDisabledWarnsOnceAndDoesNothing(t *testing.T) {
	var logs bytes.Buffer
	reporter := newQuotaReporter("", slog.New(slog.NewTextHandler(&logs, nil)))
	for i := 0; i < 3; i++ {
		reporter.report(QuotaObservation{Provider: "codex"})
	}
	if got := strings.Count(logs.String(), "quota reporting is disabled"); got != 1 {
		t.Fatalf("disabled warnings = %d, logs = %q", got, logs.String())
	}
}

func TestQuotaReportingFailureDoesNotChangeRelay(t *testing.T) {
	closedBroker := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	brokerURL := closedBroker.URL
	closedBroker.Close()
	reporter := newQuotaReporter(brokerURL, slog.New(slog.NewTextHandler(io.Discard, nil)))
	reporter.client.Timeout = 50 * time.Millisecond

	sec := &secretEntry{Header: "Authorization", value: "real", QuotaProvider: "codex"}
	upClient, upOrigin := net.Pipe()
	defer upClient.Close()
	go func() {
		defer upOrigin.Close()
		_, _ = http.ReadRequest(bufioNewReader(upOrigin))
		_, _ = io.WriteString(upOrigin, "HTTP/1.1 200 OK\r\nX-Codex-Primary-Used-Percent: 12\r\nContent-Length: 2\r\n\r\nok")
	}()
	guest := "GET http://api.example.com/test HTTP/1.1\r\nHost: api.example.com\r\nAuthorization: guest\r\nConnection: close\r\n\r\n"
	var back bytes.Buffer
	p := &proxy{logger: slog.New(slog.NewTextHandler(io.Discard, nil)), quotaReporter: reporter}
	p.swapPump(bufioNewReader(strings.NewReader(guest)), &back, nil, upClient, "api.example.com", sec)
	if !strings.HasSuffix(back.String(), "ok") {
		t.Fatalf("response = %q, want relayed body", back.String())
	}
}

// bufioNewReader keeps the relay test's long setup readable.
func bufioNewReader(r io.Reader) *bufio.Reader { return bufio.NewReader(r) }

func TestLoadSecretsRejectsUnknownQuotaProvider(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	previousExit := exitFn
	t.Cleanup(func() { exitFn = previousExit })
	exits := 0
	exitFn = func(int) { exits++ }
	t.Setenv("TOKEN", "secret")
	t.Setenv("EGRESS_SECRETS", `[{"header":"Authorization","env":"TOKEN","egressTo":["api.example.com"],"quotaProvider":"gemini"}]`)
	if got := loadSecretsWithBroker(logger, ""); got != nil {
		t.Fatalf("secrets = %#v, want nil", got)
	}
	if exits != 1 {
		t.Fatalf("exit calls = %d, want 1", exits)
	}
}
