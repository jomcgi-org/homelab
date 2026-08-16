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

func TestRewriteToPlaintextHTTPDowngradesHTTPS(t *testing.T) {
	parsed, err := url.Parse("https://jomcgi.dev/agents")
	if err != nil {
		t.Fatalf("url.Parse: %v", err)
	}
	if err := rewriteToPlaintextHTTP(parsed); err != nil {
		t.Fatalf("rewriteToPlaintextHTTP: %v", err)
	}
	if got := parsed.String(); got != "http://jomcgi.dev/agents" {
		t.Fatalf("rewritten url = %q, want http://jomcgi.dev/agents", got)
	}
}

func TestRewriteToPlaintextHTTPKeepsHTTP(t *testing.T) {
	parsed, err := url.Parse("http://jomcgi.dev/agents")
	if err != nil {
		t.Fatalf("url.Parse: %v", err)
	}
	if err := rewriteToPlaintextHTTP(parsed); err != nil {
		t.Fatalf("rewriteToPlaintextHTTP: %v", err)
	}
	if got := parsed.String(); got != "http://jomcgi.dev/agents" {
		t.Fatalf("rewritten url = %q, want http://jomcgi.dev/agents", got)
	}
}

func TestRewriteToPlaintextHTTPRejectsOtherScheme(t *testing.T) {
	parsed, err := url.Parse("ftp://jomcgi.dev/agents")
	if err != nil {
		t.Fatalf("url.Parse: %v", err)
	}
	err = rewriteToPlaintextHTTP(parsed)
	if !errors.Is(err, errValidation) {
		t.Fatalf("rewriteToPlaintextHTTP(ftp) err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestAppliesDefaultsAndDowngradesScheme(t *testing.T) {
	validated, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/agents"})
	if err != nil {
		t.Fatalf("validateScreenshotRequest: %v", err)
	}
	if validated.navigateURL != "http://jomcgi.dev/agents" {
		t.Fatalf("navigateURL = %q, want http://jomcgi.dev/agents", validated.navigateURL)
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
	_, err := validateScreenshotRequest(screenshotRequest{})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsSchemelessURL(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "jomcgi.dev/agents"})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsOversizedWidth(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", Width: maxDimension + 1, Height: defaultHeight})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
	if !strings.Contains(err.Error(), "width") {
		t.Fatalf("err = %v, want a message naming width", err)
	}
}

func TestValidateScreenshotRequestRejectsOversizedHeight(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", Width: defaultWidth, Height: maxDimension + 1})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
	if !strings.Contains(err.Error(), "height") {
		t.Fatalf("err = %v, want a message naming height", err)
	}
}

func TestValidateScreenshotRequestRejectsNegativeWidth(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", Width: -1})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestRejectsUnknownWaitUntil(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", WaitUntil: "networkidle"})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err = %v, want errValidation", err)
	}
}

func TestValidateScreenshotRequestAcceptsDOMContentLoaded(t *testing.T) {
	validated, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", WaitUntil: "domcontentloaded"})
	if err != nil {
		t.Fatalf("validateScreenshotRequest: %v", err)
	}
	if validated.waitUntilEvent != "Page.domContentEventFired" {
		t.Fatalf("waitUntilEvent = %q, want Page.domContentEventFired", validated.waitUntilEvent)
	}
}

func TestValidateScreenshotRequestRejectsTimeoutOutOfBounds(t *testing.T) {
	_, err := validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", TimeoutMs: maxNavigateTimeoutMs + 1})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err (too large) = %v, want errValidation", err)
	}
	_, err = validateScreenshotRequest(screenshotRequest{URL: "https://jomcgi.dev/", TimeoutMs: 1})
	if !errors.Is(err, errValidation) {
		t.Fatalf("err (too small) = %v, want errValidation", err)
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
