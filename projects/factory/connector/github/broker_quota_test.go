package github

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestBrokerQuotaReadsTheBrokerReading(t *testing.T) {
	answer := `{"exhausted":true,"resets_at":"2026-11-23T12:01:00Z","received_at":"2026-11-16T12:00:00.5Z"}`
	status := http.StatusOK
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != DefaultQuotaPath || r.Header.Get("Authorization") != "Bearer sa-token" {
			t.Errorf("call %s %s with %q", r.Method, r.URL.Path, r.Header.Get("Authorization"))
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(answer))
	}))
	defer srv.Close()
	b := &BrokerQuota{URL: srv.URL + "/", Bearer: staticBearer("sa-token"), HTTP: srv.Client()}
	ctx := context.Background()

	got, err := b.Quota(ctx)
	want := QuotaReading{
		Exhausted:  true,
		ResetsAt:   time.Date(2026, 11, 23, 12, 1, 0, 0, time.UTC),
		ReceivedAt: time.Date(2026, 11, 16, 12, 0, 0, 5e8, time.UTC),
	}
	if err != nil || !got.ResetsAt.Equal(want.ResetsAt) || !got.ReceivedAt.Equal(want.ReceivedAt) || !got.Exhausted {
		t.Fatalf("reading = %+v, %v", got, err)
	}

	// Before its first reading the broker reports nothing exhausted.
	answer = `{"exhausted":false}`
	if got, err := b.Quota(ctx); err != nil || got != (QuotaReading{}) {
		t.Fatalf("no reading = %+v, %v", got, err)
	}

	for _, tc := range []struct {
		status int
		answer string
	}{
		{http.StatusForbidden, `{"factory_error":"forbidden"}`},
		{http.StatusOK, `not json`},
		{http.StatusOK, `{"exhausted":false,"received_at":"yesterday"}`},
	} {
		status, answer = tc.status, tc.answer
		if _, err := b.Quota(ctx); err == nil {
			t.Fatalf("%d %s: no error", tc.status, tc.answer)
		}
	}
}
