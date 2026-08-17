package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestRewriteToMappedInternalRewritesPublicHosts(t *testing.T) {
	config := bothMappingsProxyConfig()
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "https public with query",
			in:   "https://jomcgi.dev/x?y=1",
			want: "http://monolith-public-frontend.monolith-public.svc.cluster.local:3000/x?y=1",
		},
		{
			name: "https private path",
			in:   "https://private.jomcgi.dev/a",
			want: "http://monolith.monolith.svc.cluster.local:3000/a",
		},
		{
			name: "mixed-case public host",
			in:   "https://JOMCGI.DEV/",
			want: "http://monolith-public-frontend.monolith-public.svc.cluster.local:3000/",
		},
		{
			name: "http public keeps path",
			in:   "http://jomcgi.dev/agents",
			want: "http://monolith-public-frontend.monolith-public.svc.cluster.local:3000/agents",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			parsed, err := url.Parse(testCase.in)
			if err != nil {
				t.Fatalf("url.Parse: %v", err)
			}
			if err := config.rewriteToMappedInternal(parsed); err != nil {
				t.Fatalf("rewriteToMappedInternal(%q): %v", testCase.in, err)
			}
			if got := parsed.String(); got != testCase.want {
				t.Fatalf("rewritten url = %q, want %q", got, testCase.want)
			}
		})
	}
}

func TestRewriteToMappedInternalRejectsUnmappedHost(t *testing.T) {
	parsed, err := url.Parse("https://example.com/")
	if err != nil {
		t.Fatalf("url.Parse: %v", err)
	}
	err = bothMappingsProxyConfig().rewriteToMappedInternal(parsed)
	if !errors.Is(err, errValidation) {
		t.Fatalf("rewriteToMappedInternal(example.com) err = %v, want errValidation", err)
	}
	if !strings.Contains(err.Error(), "example.com") {
		t.Fatalf("err = %v, want a message naming the unmapped host", err)
	}
}

func TestRewriteToMappedInternalRejectsOtherScheme(t *testing.T) {
	parsed, err := url.Parse("ftp://jomcgi.dev/agents")
	if err != nil {
		t.Fatalf("url.Parse: %v", err)
	}
	err = bothMappingsProxyConfig().rewriteToMappedInternal(parsed)
	if !errors.Is(err, errValidation) {
		t.Fatalf("rewriteToMappedInternal(ftp) err = %v, want errValidation", err)
	}
}

func TestRewriteToMappedInternalFailsClosedOnEmptyConfig(t *testing.T) {
	parsed, err := url.Parse("https://jomcgi.dev/x")
	if err != nil {
		t.Fatalf("url.Parse: %v", err)
	}
	err = (ProxyConfig{}).rewriteToMappedInternal(parsed)
	if !errors.Is(err, errValidation) {
		t.Fatalf("empty ProxyConfig rewrite err = %v, want errValidation", err)
	}
	if parsed.Host != "jomcgi.dev" || parsed.Scheme != "https" {
		t.Fatalf("empty config mutated url to %q, want the original public url left in place", parsed.String())
	}
}

func TestValidateScreenshotRequestAppliesDefaultsAndRewritesToMappedInternal(t *testing.T) {
	validated, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/x?y=1"}, bothMappingsProxyConfig())
	if err != nil {
		t.Fatalf("validateScreenshotRequest: %v", err)
	}
	wantNavigate := "http://monolith-public-frontend.monolith-public.svc.cluster.local:3000/x?y=1"
	if validated.navigateURL != wantNavigate {
		t.Fatalf("navigateURL = %q, want %q", validated.navigateURL, wantNavigate)
	}
	if validated.width != defaultWidth || validated.height != defaultHeight {
		t.Fatalf("dimensions = %dx%d, want %dx%d default", validated.width, validated.height, defaultWidth, defaultHeight)
	}
	if validated.waitUntilEvent != "Page.loadEventFired" {
		t.Fatalf("waitUntilEvent = %q, want Page.loadEventFired default", validated.waitUntilEvent)
	}
	if validated.navigateTimeout != defaultNavigateTimeoutMs*time.Millisecond {
		t.Fatalf("navigateTimeout = %v, want %v default", validated.navigateTimeout, defaultNavigateTimeoutMs*time.Millisecond)
	}
}

func TestValidateScreenshotRequestRejectsMissingURL(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsSchemelessURL(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "jomcgi.dev/agents"}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsOversizedWidth(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", Width: maxDimension + 1, Height: defaultHeight}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
	if !strings.Contains(err.Error(), "width") {
		t.Fatalf("err = %v, want a message naming width", err)
	}
}

func TestValidateScreenshotRequestRejectsOversizedHeight(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", Width: defaultWidth, Height: maxDimension + 1}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
	if !strings.Contains(err.Error(), "height") {
		t.Fatalf("err = %v, want a message naming height", err)
	}
}

func TestValidateScreenshotRequestRejectsNegativeWidth(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", Width: -1}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsUnknownWaitUntil(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", WaitUntil: "networkidle"}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestAcceptsDOMContentLoaded(t *testing.T) {
	validated, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", WaitUntil: "domcontentloaded"}, bothMappingsProxyConfig())
	if err != nil {
		t.Fatalf("validateScreenshotRequest: %v", err)
	}
	if validated.waitUntilEvent != "Page.domContentEventFired" {
		t.Fatalf("waitUntilEvent = %q, want Page.domContentEventFired", validated.waitUntilEvent)
	}
}

func TestValidateScreenshotRequestRejectsTimeoutOutOfBounds(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", TimeoutMs: maxNavigateTimeoutMs + 1}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err (too large) = %v, want errValidation", err)
	}
	_, err = validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", TimeoutMs: 1}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err (too small) = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsUnmappedHost(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://example.com/"}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsFTPScheme(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "ftp://jomcgi.dev/"}, bothMappingsProxyConfig())
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestFailsClosedOnEmptyConfig(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/x"}, ProxyConfig{})
	if !errors.Is(err, errValidation) {
		t.Fatalf("empty ProxyConfig err = %v, want errValidation", err)
	}
}

func TestPublicizeFinalURLRewritesMappedDestination(t *testing.T) {
	config := bothMappingsProxyConfig()
	got := config.publicizeFinalURL("http://monolith-public-frontend.monolith-public.svc.cluster.local:3000/x")
	if got != "https://jomcgi.dev/x" {
		t.Fatalf("publicizeFinalURL = %q, want https://jomcgi.dev/x", got)
	}
	got = config.publicizeFinalURL("http://monolith.monolith.svc.cluster.local:3000/a")
	if got != "https://private.jomcgi.dev/a" {
		t.Fatalf("publicizeFinalURL(private) = %q, want https://private.jomcgi.dev/a", got)
	}
}

func TestPublicizeFinalURLLeavesOffOriginUnchanged(t *testing.T) {
	const offOrigin = "https://example.com/redirected"
	got := bothMappingsProxyConfig().publicizeFinalURL(offOrigin)
	if got != offOrigin {
		t.Fatalf("publicizeFinalURL(off-origin) = %q, want unchanged %q", got, offOrigin)
	}
}

func TestScreenshotHandlerFailsClosedOnEmptyConfig(t *testing.T) {
	handler := screenshotHandler(discardLogger(), ProxyConfig{})
	recorder := httptest.NewRecorder()
	req := httptest.NewRequest("POST", screenshotPath, strings.NewReader(`{"url":"https://jomcgi.dev/x"}`))
	handler.ServeHTTP(recorder, req)
	if recorder.Code != 400 {
		t.Fatalf("status = %d, want 400", recorder.Code)
	}
}

func TestParseScreenshotRequestRejectsUnknownFields(t *testing.T) {
	body := strings.NewReader(`{"url":"https://jomcgi.dev/","not_a_field":true}`)
	_, err := parseScreenshotRequest(body)
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestParseScreenshotRequestRejectsMalformedJSON(t *testing.T) {
	body := strings.NewReader(`{"url":`)
	_, err := parseScreenshotRequest(body)
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestWriteScreenshotErrorMapsToStatus(t *testing.T) {
	logger := discardLogger()
	cases := []struct {
		name       string
		err        error
		wantStatus int
	}{
		{"validation", errValidation, 400},
		{"too large", errCaptureTooLarge, 413},
		{"navigation failed", errNavigationFailed, 502},
		{"navigation timeout", errNavigationTimeout, 504},
		{"context deadline", context.DeadlineExceeded, 504},
		{"unmapped", errors.New("boom"), 500},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			writeScreenshotError(recorder, logger, testCase.err)
			if recorder.Code != testCase.wantStatus {
				t.Fatalf("status = %d, want %d", recorder.Code, testCase.wantStatus)
			}
		})
	}
}

func TestHandlerCapClampsToAbsoluteCeiling(t *testing.T) {
	got := handlerCap(maxNavigateTimeoutMs * time.Millisecond)
	if got != maxHandlerCap {
		t.Fatalf("handlerCap(max navigate timeout) = %v, want the %v ceiling", got, maxHandlerCap)
	}
}

func TestHandlerCapStaysStrictlyAboveNavigateTimeoutWhenNotClamped(t *testing.T) {
	navigateTimeout := 5 * time.Second
	got := handlerCap(navigateTimeout)
	if got <= navigateTimeout {
		t.Fatalf("handlerCap(%v) = %v, want strictly greater than the navigate timeout it wraps", navigateTimeout, got)
	}
	if got != navigateTimeout+handlerOverhead {
		t.Fatalf("handlerCap(%v) = %v, want %v", navigateTimeout, got, navigateTimeout+handlerOverhead)
	}
}
