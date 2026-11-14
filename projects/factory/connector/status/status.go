// Package status serves the connector's read-only landing snapshot.
package status

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/config"
	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const RefreshInterval = 30 * time.Second
const ReadTimeout = 20 * time.Second

// WriterTransport checks the writer immediately before each status HTTP call,
// including token fetches, redirects and retries. These reads send no kagent
// messages and therefore create no delivery attempts or message IDs.
type WriterTransport struct {
	Check func(context.Context) error
}

func (t *WriterTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if err := r.Context().Err(); err != nil {
		return nil, err
	}
	if t.Check == nil {
		return nil, store.ErrNotWriter
	}
	if err := t.Check(r.Context()); err != nil {
		return nil, err
	}
	return http.DefaultTransport.RoundTrip(r)
}

type NeedsYou struct {
	Key       string  `json:"key"`
	Title     string  `json:"title"`
	Priority  *string `json:"priority"`
	URL       string  `json:"url"`
	CreatedAt string  `json:"createdAt"`
}

type InFlight struct {
	Key          string           `json:"key"`
	Title        string           `json:"title"`
	State        string           `json:"state"`
	ReviewRounds int              `json:"reviewRounds"`
	PR           *github.PRStatus `json:"pr"`
}

type PRLink struct {
	Number int    `json:"number"`
	URL    string `json:"url"`
}

type Shipped struct {
	Key      string    `json:"key"`
	Title    string    `json:"title"`
	PR       PRLink    `json:"pr"`
	MergedAt time.Time `json:"mergedAt"`
}

type Snapshot struct {
	NeedsYou    []NeedsYou `json:"needsYou"`
	InFlight    []InFlight `json:"inFlight"`
	Shipped     []Shipped  `json:"shipped"`
	GeneratedAt time.Time  `json:"generatedAt"`
}

type Store interface {
	StatusItems(context.Context, []string) ([]store.StatusItem, error)
}

type Plan interface {
	NeedsYou(context.Context, string, string) ([]itsaplan.IssueSummary, error)
	GetIssueSummary(context.Context, int64) (*itsaplan.IssueSummary, error)
}

type GitHub interface {
	Status(context.Context, string, int, github.CheckRunFilter) (*github.PRStatus, error)
}

type Source struct {
	Store    Store
	Plan     Plan
	GitHub   GitHub
	Bindings []config.Binding
	IssueURL string
}

func (s *Source) Load(ctx context.Context) (*Snapshot, error) {
	out := &Snapshot{NeedsYou: []NeedsYou{}, InFlight: []InFlight{}, Shipped: []Shipped{}}
	bindings := map[string]config.Binding{}
	names := make([]string, 0, len(s.Bindings))
	for _, b := range s.Bindings {
		bindings[b.Name] = b
		names = append(names, b.Name)
		issues, err := s.Plan.NeedsYou(ctx, b.ItsaplanProject, "joe")
		if err != nil {
			return nil, err
		}
		for _, issue := range issues {
			link := strings.NewReplacer("{issueId}", strconv.FormatInt(issue.ID, 10),
				"{issue}", url.PathEscape(issue.Identifier)).Replace(s.IssueURL)
			out.NeedsYou = append(out.NeedsYou, NeedsYou{Key: issue.Identifier, Title: issue.Title,
				Priority: issue.Priority, URL: link, CreatedAt: issue.CreatedAt})
		}
	}
	items, err := s.Store.StatusItems(ctx, names)
	if err != nil {
		return nil, err
	}
	// Eight workers bound parallel reads. The store caps the input at 110
	// items, and each needs one title read and at most one GitHub query.
	var mu sync.Mutex
	var firstErr error
	var wg sync.WaitGroup
	jobs := make(chan store.StatusItem)
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range jobs {
				b, ok := bindings[item.BindingName]
				if !ok {
					continue
				}
				issue, err := s.Plan.GetIssueSummary(ctx, item.IssueID)
				var pr *github.PRStatus
				if err == nil && item.PRNumber != 0 && item.MergedAt == nil && s.GitHub != nil {
					var reviewer github.CheckRunFilter
					if b.ReviewerCheck != nil {
						reviewer = github.CheckRunFilter{Name: b.ReviewerCheck.Name, AppID: b.ReviewerCheck.AppID}
					}
					pr, err = s.GitHub.Status(ctx, b.Owner+"/"+b.Repo, item.PRNumber, reviewer)
				}
				mu.Lock()
				if err != nil {
					if firstErr == nil {
						firstErr = err
					}
				} else if item.MergedAt != nil {
					out.Shipped = append(out.Shipped, Shipped{Key: item.Key, Title: issue.Title,
						PR: PRLink{Number: item.PRNumber, URL: item.PRURL}, MergedAt: *item.MergedAt})
				} else {
					if item.PRNumber != 0 && pr == nil {
						pr = &github.PRStatus{Number: item.PRNumber, URL: item.PRURL, CI: "unknown", Review: "unknown", Queue: "unknown"}
					}
					out.InFlight = append(out.InFlight, InFlight{Key: item.Key, Title: issue.Title,
						State: item.State, ReviewRounds: item.ReviewRounds, PR: pr})
				}
				mu.Unlock()
			}
		}()
	}
	for _, item := range items {
		select {
		case jobs <- item:
		case <-ctx.Done():
		}
	}
	close(jobs)
	wg.Wait()
	if firstErr != nil {
		return nil, firstErr
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	sort.Slice(out.NeedsYou, func(i, j int) bool { return out.NeedsYou[i].CreatedAt < out.NeedsYou[j].CreatedAt })
	sort.Slice(out.InFlight, func(i, j int) bool { return out.InFlight[i].Key < out.InFlight[j].Key })
	sort.Slice(out.Shipped, func(i, j int) bool { return out.Shipped[i].MergedAt.After(out.Shipped[j].MergedAt) })
	out.GeneratedAt = time.Now().UTC()
	return out, nil
}

// Handler refreshes in the background, independent of visitors. HTTP never
// starts upstream work or waits for it. Failed refreshes clear the snapshot;
// during a refresh the previous snapshot remains readable for at most the
// read deadline beyond its expiry. No upstream error text is exposed or logged.
type Handler struct {
	Load         func(context.Context) (*Snapshot, error)
	mu           sync.RWMutex
	body         []byte
	expiresAt    time.Time
	refreshUntil time.Time
	now          func() time.Time
}

func (h *Handler) clock() time.Time {
	if h.now != nil {
		return h.now()
	}
	return time.Now()
}

func (h *Handler) refresh(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, ReadTimeout)
	defer cancel()
	h.mu.Lock()
	h.refreshUntil = h.clock().Add(ReadTimeout)
	h.mu.Unlock()
	snapshot, err := h.Load(ctx)
	if err == nil {
		err = ctx.Err()
	}
	var body []byte
	if err == nil {
		body, err = json.Marshal(snapshot)
	}
	if err != nil {
		body = nil
	}
	h.mu.Lock()
	h.body = body
	h.expiresAt = h.clock().Add(RefreshInterval)
	h.refreshUntil = time.Time{}
	h.mu.Unlock()
}

func (h *Handler) Run(ctx context.Context) {
	ticker := time.NewTicker(RefreshInterval)
	defer ticker.Stop()
	h.run(ctx, ticker.C)
}

func (h *Handler) run(ctx context.Context, ticks <-chan time.Time) {
	defer func() {
		h.mu.Lock()
		h.body = nil
		h.refreshUntil = time.Time{}
		h.mu.Unlock()
	}()
	if ctx.Err() != nil {
		return
	}
	h.refresh(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticks:
			if ctx.Err() != nil {
				return
			}
			h.refresh(ctx)
		}
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if r.URL.Path != "/status" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET")
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	h.mu.RLock()
	body := h.body
	now := h.clock()
	expired := !now.Before(h.expiresAt) && !now.Before(h.refreshUntil)
	h.mu.RUnlock()
	if body == nil || expired {
		http.Error(w, "Status unavailable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(body)
}
