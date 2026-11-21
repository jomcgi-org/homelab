// Package status serves the connector's read-only landing snapshot.
package status

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
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

const (
	RefreshInterval = 30 * time.Second
	ReadTimeout     = 20 * time.Second
)

type NeedsYou struct {
	ChoiceRevision string   `json:"choiceRevision,omitempty"`
	Choices        []string `json:"choices,omitempty"`
	Key            string   `json:"key"`
	Title          string   `json:"title"`
	Priority       *string  `json:"priority"`
	URL            string   `json:"url"`
	CreatedAt      string   `json:"createdAt"`
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

// Snapshot.NeedsYou is nil (JSON null) when a bound project has no member
// `joe`; In flight and Shipped recently still render.
type Snapshot struct {
	NeedsYou      []NeedsYou `json:"needsYou"`
	InFlight      []InFlight `json:"inFlight"`
	InFlightTotal int        `json:"inFlightTotal,omitempty"`
	Shipped       []Shipped  `json:"shipped"`
	GeneratedAt   time.Time  `json:"generatedAt"`
}

type Store interface {
	StatusItems(context.Context, []string) ([]store.StatusItem, int, error)
}

type Plan interface {
	NeedsYou(context.Context, string, string) ([]itsaplan.IssueSummary, error)
	GetIssueSummary(context.Context, int64) (*itsaplan.IssueSummary, error)
}

type GitHub interface {
	StatusesForLanding(context.Context, string, []int, github.CheckRunFilter) (map[int]*github.PRStatus, error)
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
	ownerMissing := false
	for _, b := range s.Bindings {
		bindings[b.Name] = b
		names = append(names, b.Name)
		issues, err := s.Plan.NeedsYou(ctx, b.ItsaplanProject, "joe")
		if errors.Is(err, itsaplan.ErrNotMember) {
			ownerMissing = true
			continue
		}
		if err != nil {
			return nil, &stageError{"needs_you", err}
		}
		for _, issue := range issues {
			link := strings.NewReplacer("{issueId}", strconv.FormatInt(issue.ID, 10),
				"{issue}", url.PathEscape(issue.Identifier)).Replace(s.IssueURL)
			out.NeedsYou = append(out.NeedsYou, NeedsYou{
				Key: issue.Identifier, Title: issue.Title, Choices: issue.Choices, ChoiceRevision: issue.ChoiceRevision,
				Priority: issue.Priority, URL: link, CreatedAt: issue.CreatedAt,
			})
		}
	}
	if ownerMissing {
		// A partial list would undercount Joe's tasks.
		out.NeedsYou = nil
	}
	items, liveTotal, err := s.Store.StatusItems(ctx, names)
	if err != nil {
		return nil, &stageError{"work_items", err}
	}
	// Bound the total across bindings before making any GitHub requests.
	// Four batches per 30-second refresh cost 480 primary points/hour, or
	// at most 960 with one auth retry each, leaving delivery most of its quota.
	prNumbers := map[string][]int{}
	for _, item := range items {
		if item.PRNumber != 0 && item.MergedAt == nil {
			prNumbers[item.BindingName] = append(prNumbers[item.BindingName], item.PRNumber)
		}
	}
	batches := 0
	for _, numbers := range prNumbers {
		batches += (len(numbers) + github.StatusBatchSize - 1) / github.StatusBatchSize
	}
	if batches > 4 {
		return nil, &stageError{"github_budget", fmt.Errorf("status: GitHub query budget exceeded")}
	}
	prs := map[string]map[int]*github.PRStatus{}
	if s.GitHub != nil {
		for _, b := range s.Bindings {
			numbers := prNumbers[b.Name]
			if len(numbers) == 0 {
				continue
			}
			var reviewer github.CheckRunFilter
			if b.ReviewerCheck != nil {
				reviewer = github.CheckRunFilter{Name: b.ReviewerCheck.Name, AppID: b.ReviewerCheck.AppID}
			}
			prs[b.Name], err = s.GitHub.StatusesForLanding(ctx, b.Owner+"/"+b.Repo, numbers, reviewer)
			if err != nil {
				return nil, &stageError{"github", err}
			}
		}
	}
	// Eight workers bound title reads. The store caps the input at 110 items.
	var mu sync.Mutex
	var firstErr error
	var wg sync.WaitGroup
	jobs := make(chan store.StatusItem)
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for item := range jobs {
				_, ok := bindings[item.BindingName]
				if !ok {
					continue
				}
				issue, err := s.Plan.GetIssueSummary(ctx, item.IssueID)
				pr := prs[item.BindingName][item.PRNumber]
				mu.Lock()
				if err != nil {
					if firstErr == nil {
						firstErr = err
					}
				} else if item.MergedAt != nil {
					out.Shipped = append(out.Shipped, Shipped{
						Key: item.Key, Title: issue.Title,
						PR: PRLink{Number: item.PRNumber, URL: item.PRURL}, MergedAt: *item.MergedAt,
					})
				} else {
					if item.PRNumber != 0 && pr == nil {
						pr = &github.PRStatus{Number: item.PRNumber, URL: item.PRURL, CI: "unknown", Review: "unknown", Queue: "unknown"}
					}
					out.InFlight = append(out.InFlight, InFlight{
						Key: item.Key, Title: issue.Title,
						State: item.State, ReviewRounds: item.ReviewRounds, PR: pr,
					})
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
		return nil, &stageError{"titles", firstErr}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if liveTotal > len(out.InFlight) {
		out.InFlightTotal = liveTotal
	}
	sort.Slice(out.NeedsYou, func(i, j int) bool { return out.NeedsYou[i].CreatedAt < out.NeedsYou[j].CreatedAt })
	sort.Slice(out.InFlight, func(i, j int) bool { return out.InFlight[i].Key < out.InFlight[j].Key })
	sort.Slice(out.Shipped, func(i, j int) bool { return out.Shipped[i].MergedAt.After(out.Shipped[j].MergedAt) })
	out.GeneratedAt = time.Now().UTC()
	return out, nil
}

// stageError names the read that failed. Logs carry only the stage.
type stageError struct {
	stage string
	err   error
}

func (e *stageError) Error() string { return e.stage + ": " + e.err.Error() }
func (e *stageError) Unwrap() error { return e.err }

// Handler refreshes in the background, independent of visitors. HTTP never
// starts upstream work or waits for it. Failed refreshes clear the snapshot;
// during a refresh the previous snapshot remains readable for at most the
// read deadline beyond its expiry. Log receives the failing stage and whether
// it timed out; no upstream error text is exposed or logged.
type Handler struct {
	Load         func(context.Context) (*Snapshot, error)
	Log          *slog.Logger
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
		if h.Log != nil {
			stage := "other"
			var se *stageError
			if errors.As(err, &se) {
				stage = se.stage
			}
			h.Log.Warn("status refresh failed", "stage", stage,
				"timeout", errors.Is(err, context.DeadlineExceeded))
		}
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
