package github

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestLandingStatusBatchesFortyTwoAndOneHundredPRs(t *testing.T) {
	for _, count := range []int{42, 100} {
		t.Run(strconv.Itoa(count), func(t *testing.T) {
			var mu sync.Mutex
			reads := 0
			wantReads := (count + 24) / 25
			allStarted := make(chan struct{})
			seen := map[int]bool{}
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var request struct{ Query string }
				if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
					t.Error(err)
				}
				aliases := regexp.MustCompile(`p(\d+): pullRequest\(number: (\d+)\)`).FindAllStringSubmatch(request.Query, -1)
				if len(aliases) == 0 || len(aliases) > 25 || strings.Count(request.Query, "last: 1") != 3*len(aliases) {
					t.Errorf("unbounded query: %d aliases", len(aliases))
				}
				prs := map[string]any{}
				mu.Lock()
				for _, alias := range aliases {
					number, _ := strconv.Atoi(alias[1])
					if alias[1] != alias[2] || seen[number] {
						t.Errorf("wrong or repeated alias: %v", alias)
					}
					seen[number] = true
					prs["p"+alias[1]] = map[string]any{"number": number, "url": fmt.Sprintf("https://github.com/org/repo/pull/%d", number)}
				}
				reads++
				if reads == wantReads {
					close(allStarted)
				}
				mu.Unlock()
				// Each batch starts before any batch can reply.
				select {
				case <-allStarted:
				case <-r.Context().Done():
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"data": map[string]any{"repository": prs}})
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			numbers := make([]int, count)
			for i := range numbers {
				numbers[i] = count - i
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			out, err := c.StatusesForLanding(ctx, "org/repo", numbers, CheckRunFilter{AppID: 123, Name: "factory review"})
			mu.Lock()
			gotReads := reads
			mu.Unlock()
			if err != nil || len(out) != count || gotReads != wantReads {
				t.Fatalf("PRs=%d reads=%d err=%v", len(out), gotReads, err)
			}
			if numbers[0] != count {
				t.Fatal("modified caller's input")
			}
		})
	}
}

func TestLandingStatusRejectsUnboundedInputBeforeSending(t *testing.T) {
	c := &Client{} // No transport or tokens should be needed.
	for _, numbers := range [][]int{make([]int, 101), {0}, {-1}} {
		if _, err := c.StatusesForLanding(context.Background(), "org/repo", numbers, CheckRunFilter{}); err == nil {
			t.Fatal("invalid input accepted")
		}
	}
}

func TestStatusUsesOneBoundedSummaryQuery(t *testing.T) {
	reads := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads++
		if r.URL.Path != "/graphql" {
			t.Errorf("path = %s", r.URL.Path)
		}
		var request struct {
			Query     string
			Variables map[string]any
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Error(err)
		}
		for _, forbidden := range []string{"body", "summary", "text", "reviews(", "reviewThreads"} {
			if strings.Contains(request.Query, forbidden) {
				t.Errorf("query reads %s", forbidden)
			}
		}
		if !strings.Contains(request.Query, "p8000: pullRequest(number: 8000)") || request.Variables["appId"] != float64(123) || request.Variables["hasReviewer"] != true {
			t.Errorf("variables = %v", request.Variables)
		}
		_, _ = w.Write([]byte(`{"data":{"repository":{"p8000":{"number":8000,"url":"https://github.com/org/repo/pull/8000","reviewDecision":"APPROVED","mergeQueueEntry":{"state":"QUEUED"},"commits":{"nodes":[{"commit":{"statusCheckRollup":{"state":"SUCCESS"},"checkSuites":{"nodes":[{"checkRuns":{"nodes":[{"status":"COMPLETED","conclusion":"FAILURE"}]}}]}}}]}}}}}`))
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	statuses, err := c.StatusesForLanding(context.Background(), "org/repo", []int{8000}, CheckRunFilter{AppID: 123, Name: "factory review"})
	if err != nil {
		t.Fatal(err)
	}
	s := statuses[8000]
	if reads != 1 || s.CI != "success" || s.Review != "failure" || s.Queue != "queued" {
		t.Fatalf("status=%+v, reads=%d", s, reads)
	}
}

func TestStatusMissingChecksAndPullRequest(t *testing.T) {
	for _, exists := range []bool{true, false} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			if exists {
				_, _ = w.Write([]byte(`{"data":{"repository":{"p1":{"number":1,"reviewDecision":"APPROVED"}}}}`))
			} else {
				_, _ = w.Write([]byte(`{"data":{"repository":{"p1":null}}}`))
			}
		}))
		c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
		statuses, err := c.StatusesForLanding(context.Background(), "org/repo", []int{1}, CheckRunFilter{})
		srv.Close()
		s := statuses[1]
		if !exists {
			if err != ErrNotFound {
				t.Fatalf("err=%v", err)
			}
		} else if err != nil || s.CI != "unknown" || s.Review != "approved" || s.Queue != "not_queued" {
			t.Fatalf("status=%+v err=%v", s, err)
		}
	}
}
