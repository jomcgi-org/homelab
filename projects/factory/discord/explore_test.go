package discord

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func TestParseExploreAcceptsOnlyPublicRepoSyntax(t *testing.T) {
	ok := []string{
		`{"repo":"jomcgi-org/homelab","question":"Where is the bridge?","research":"new"}`,
		`{"repo":"a/b.c_d-e","question":"q","research":"1501965852042330302"}`,
	}
	for _, spec := range ok {
		if _, err := ParseExplore(spec); err != nil {
			t.Errorf("ParseExplore(%s): %v", spec, err)
		}
	}
	bad := []string{
		`{"repo":"homelab","question":"q","research":"new"}`,
		`{"repo":"a/b/c","question":"q","research":"new"}`,
		`{"repo":"-a/b","question":"q","research":"new"}`,
		`{"repo":"a/..","question":"q","research":"new"}`,
		`{"repo":"a/b.git","question":"q","research":"new"}`,
		`{"repo":"https://github.com/a/b","question":"q","research":"new"}`,
		`{"repo":"a/b","question":" ","research":"new"}`,
		`{"repo":"a/b","question":"` + strings.Repeat("x", 2001) + `","research":"new"}`,
		`{"repo":"a/b","question":"q","research":"../x"}`,
		`{"repo":"a/b","question":"q","research":"123"}`,
		`{"repo":"a/b","question":"q"}`,
		`{"repo":"a/b","question":"q","research":"new","token":"x"}`,
	}
	for _, spec := range bad {
		if _, err := ParseExplore(spec); err == nil {
			t.Errorf("ParseExplore(%s) accepted", spec)
		}
	}
}

func TestTakeActionsTakesOneExploreBlock(t *testing.T) {
	answer := "Looking.\n```explore\n{\"repo\":\"a/b\",\"question\":\"q1\",\"research\":\"new\"}\n```\n" +
		"```explore\n{\"repo\":\"c/d\",\"question\":\"q2\",\"research\":\"new\"}\n```"
	text, a := TakeActions(answer)
	if a.Explore == nil || a.Explore.Repo != "a/b" || !a.Explore.New() {
		t.Fatalf("explore = %+v", a.Explore)
	}
	if !strings.Contains(text, "c/d") || strings.Contains(text, "a/b") {
		t.Errorf("text = %q", text)
	}
}

func TestGitHubPublicTrustsOnlyAPublicAnswer(t *testing.T) {
	answers := map[string]string{
		"/repos/o/public":  `{"full_name":"o/public","private":false,"visibility":"public","default_branch":"main","size":42}`,
		"/repos/o/private": `{"full_name":"o/private","private":true,"visibility":"private"}`,
		"/repos/o/nofield": `{"full_name":"o/nofield","visibility":"public"}`,
		"/repos/o/odd":     `{"full_name":"o/odd/../x","private":false,"visibility":"public"}`,
	}
	var auth []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = append(auth, r.Header.Get("Authorization"))
		body, ok := answers[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Write([]byte(body))
	}))
	defer srv.Close()
	g := &GitHubPublic{BaseURL: srv.URL}
	repo, err := g.Public(context.Background(), "o/public")
	if err != nil || repo.FullName != "o/public" || repo.DefaultBranch != "main" || repo.SizeKB != 42 {
		t.Errorf("public = %+v, %v", repo, err)
	}
	for _, name := range []string{"o/private", "o/nofield", "o/odd", "o/missing", "bad"} {
		if _, err := g.Public(context.Background(), name); !errors.Is(err, ErrNotPublic) {
			t.Errorf("%s: err = %v", name, err)
		}
	}
	for _, a := range auth {
		if a != "" {
			t.Errorf("sent credentials %q", a)
		}
	}
}

func TestExploreChunksSplitAndClip(t *testing.T) {
	if got := ExploreChunks("H", "short"); len(got) != 1 || got[0] != "H\nshort" {
		t.Errorf("short = %q", got)
	}
	long := strings.Repeat(strings.Repeat("é", 99)+"\n", 200) // 20,000 runes
	got := ExploreChunks("H", long)
	if len(got) != maxExploreChunks || !strings.HasSuffix(got[len(got)-1], "(clipped; ask a narrower follow-up)") {
		t.Fatalf("chunks = %d", len(got))
	}
	for _, c := range got {
		if n := len([]rune(c)); n > maxExploreChunk {
			t.Errorf("chunk of %d runes", n)
		}
	}
}

// lockedReplies records replies and actions from any goroutine.
type lockedReplies struct {
	mu      sync.Mutex
	posts   []string // channel: text
	actions []string
	threads int
}

func (f *lockedReplies) Send(ctx context.Context, c string, r Reply) error {
	_, err := f.Post(ctx, c, r)
	return err
}

func (f *lockedReplies) Post(_ context.Context, c string, r Reply) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, c+": "+r.Components[0].Content)
	return fmt.Sprintf("r%d", len(f.posts)), nil
}

func (f *lockedReplies) React(context.Context, string, string, string) error { return nil }
func (f *lockedReplies) Pin(context.Context, string, string) error           { return nil }

func (f *lockedReplies) StartThread(_ context.Context, c, m, name string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.actions = append(f.actions, "thread "+c+"/"+m+" "+name)
	f.threads++
	return fmt.Sprintf("thread-%d", f.threads), nil
}

func (f *lockedReplies) all() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.posts...)
}

type fakeRepos map[string]Repository

func (f fakeRepos) Public(_ context.Context, repo string) (Repository, error) {
	r, ok := f[repo]
	if !ok {
		return Repository{}, ErrNotPublic
	}
	return r, nil
}

// exploreSessions is an explorer's kagent sessions.
type exploreSessions struct {
	mu      sync.Mutex
	list    []*kagent.Session
	named   map[string]string
	deleted []string
}

func (f *exploreSessions) ListSessions(context.Context, kagent.AgentRef, string) ([]*kagent.Session, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]*kagent.Session(nil), f.list...), "", nil
}

func (f *exploreSessions) NameSession(_ context.Context, id, name string) (*kagent.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.named == nil {
		f.named = map[string]string{}
	}
	f.named[id] = name
	return nil, nil
}

func (f *exploreSessions) DeleteSession(_ context.Context, id string) (*kagent.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deleted = append(f.deleted, id)
	return nil, nil
}

var explorerRef = kagent.AgentRef{Namespace: "discord-explorer", Name: "explorer"}

func newExploreBridge(answer string) (*Bridge, *lockedReplies, *fakeOperator, *exploreSessions) {
	b, _, _ := newBridge(&fakeAgent{answer: answer})
	rep := &lockedReplies{}
	b.Replies = rep
	b.Config.Explore = ExploreConfig{
		Agent: explorerRef, Limit: Limit{Count: 2, Window: time.Hour}, Concurrency: 1,
		Timeout: 5 * time.Minute, MaxSessions: 4, TTL: 72 * time.Hour, MaxRepoKB: 1 << 20,
	}
	op, sessions := &fakeOperator{}, &exploreSessions{}
	b.Explorer = &Explorer{Agent: op, Sessions: sessions, Ref: explorerRef}
	b.Repos = fakeRepos{"jomcgi-org/homelab": {FullName: "jomcgi-org/homelab", DefaultBranch: "main", SizeKB: 900_000}}
	return b, rep, op, sessions
}

func waitPosts(t *testing.T, rep *lockedReplies, n int) []string {
	t.Helper()
	waitFor(t, func() bool { return len(rep.all()) >= n })
	return rep.all()
}

const exploreNew = "On it.\n```explore\n{\"repo\":\"jomcgi-org/homelab\",\"question\":\"Where does the bridge parse actions?\",\"research\":\"new\"}\n```"

func TestANewResearchRunsInItsThreadAndFeedsBosun(t *testing.T) {
	b, rep, op, sessions := newExploreBridge(exploreNew)
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); b.Wait() }()
	b.start(ctx)
	b.Turn(ctx, tagged("1501965852042330301", "look into homelab"))
	posts := waitPosts(t, rep, 3)
	want := []string{
		channel + ": On it.",
		"thread-1: Researching jomcgi-org/homelab (research 1501965852042330301). I'll post here when it's done.",
		"thread-1: **Research 1501965852042330301** · jomcgi-org/homelab\nnoted",
	}
	if strings.Join(posts, "|") != strings.Join(want, "|") {
		t.Fatalf("posts = %q", posts)
	}
	// The research thread starts from Bosun's reply.
	if rep.actions[0] != "thread "+channel+"/r1 Research: jomcgi-org/homelab" {
		t.Errorf("actions = %v", rep.actions)
	}
	prompts := op.wait(1)
	var data struct {
		Repo, CloneURL, Research, Question string
	}
	raw := prompts[0][strings.Index(prompts[0], "{"):]
	_ = json.Unmarshal([]byte(strings.ReplaceAll(raw, "clone_url", "cloneurl")), &data)
	if data.Repo != "jomcgi-org/homelab" || data.CloneURL != "https://github.com/jomcgi-org/homelab.git" || data.Research != "1501965852042330301" {
		t.Errorf("explorer prompt = %s", prompts[0])
	}
	// The session is keyed and named by the research ID.
	waitFor(t, func() bool {
		sessions.mu.Lock()
		defer sessions.mu.Unlock()
		return sessions.named["ctx-1501965852042330301"] == "1501965852042330301"
	})

	// Bosun's next turn in the thread gets the answer, once.
	agent := &fakeAgent{answer: "ok"}
	b.Agent = agent
	m := tagged("1501965852042330302", "and?")
	m.ChannelID = "thread-1"
	b.Turn(ctx, m)
	m.ID = "1501965852042330303"
	b.Turn(ctx, m)
	if !strings.Contains(agent.prompts[0], `"research_results":[{"research":"1501965852042330301","repo":"jomcgi-org/homelab"`) ||
		strings.Contains(agent.prompts[1], `"research_results":`) {
		t.Errorf("prompts = %q", agent.prompts)
	}
}

func TestAFollowUpWakesTheSameSession(t *testing.T) {
	follow := "```explore\n{\"repo\":\"jomcgi-org/homelab\",\"question\":\"And the poll?\",\"research\":\"1501965852042330301\"}\n```"
	b, rep, op, _ := newExploreBridge(follow)
	op.Session(context.Background(), "1501965852042330301") // An earlier turn made it.
	op.turns["ctx-1501965852042330301"] = 1
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); b.Wait() }()
	b.start(ctx)
	m := tagged("1501965852042330309", "follow up")
	m.ChannelID = "thread-9"
	b.Turn(ctx, m)
	posts := waitPosts(t, rep, 2)
	// No new thread: a follow-up stays where Joe asked.
	if len(rep.actions) != 0 || !strings.HasPrefix(posts[0], "thread-9: Researching") || !strings.HasPrefix(posts[1], "thread-9: **Research 1501965852042330301**") {
		t.Errorf("posts = %q, actions = %v", posts, rep.actions)
	}
	op.wait(1)
	if op.contexts[0] != "ctx-1501965852042330301" {
		t.Errorf("contexts = %v", op.contexts)
	}
}

func TestResearchRefusesPrivateReposAndHoldsItsLimit(t *testing.T) {
	private := strings.ReplaceAll(exploreNew, "jomcgi-org/homelab", "jomcgi-org/secret")
	b, rep, op, _ := newExploreBridge(private)
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); b.Wait() }()
	b.start(ctx)
	b.Turn(ctx, tagged("1501965852042330301", "x"))
	posts := waitPosts(t, rep, 3)
	if !strings.Contains(posts[2], "public GitHub repositories only; jomcgi-org/secret") {
		t.Errorf("posts = %q", posts)
	}
	// Limit 2 per hour: the private check spent one.
	b.Agent = &fakeAgent{answer: exploreNew}
	b.Turn(ctx, tagged("1501965852042330302", "x"))
	waitPosts(t, rep, 6)
	b.Turn(ctx, tagged("1501965852042330303", "x"))
	posts = waitPosts(t, rep, 8)
	if posts[7] != channel+": "+ReplyExploreLimited {
		t.Errorf("posts = %q", posts)
	}
	if got := len(op.wait(1)); got != 1 {
		t.Errorf("explorer turns = %d", got)
	}
}

func TestSweepDeletesOldAndExtraSessionsButNotBusyOnes(t *testing.T) {
	now := time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC)
	s := func(id, name string, age time.Duration) *kagent.Session {
		return &kagent.Session{ID: id, Name: name, AgentNamespace: explorerRef.Namespace, AgentName: explorerRef.Name, CreatedAt: now.Add(-age)}
	}
	sessions := &exploreSessions{list: []*kagent.Session{
		s("old", "111111111111111111", 100*time.Hour),
		s("busy", "222222222222222222", 100*time.Hour),
		s("a", "333333333333333333", 3*time.Hour),
		s("b", "444444444444444444", 2*time.Hour),
		s("c", "", time.Hour),
		{ID: "other", AgentNamespace: "x", AgentName: "y", CreatedAt: now.Add(-1000 * time.Hour)},
	}}
	var forgot []string
	e := &Explorer{Sessions: sessions, Ref: explorerRef, Now: func() time.Time { return now }, Forget: func(k string) { forgot = append(forgot, k) }}
	e.claim("222222222222222222")
	// A recent turn keeps "a" young.
	e.lastUsed["333333333333333333"] = now
	n, err := e.Sweep(context.Background(), 72*time.Hour, 2)
	if err != nil || n != 2 {
		t.Fatalf("Sweep = %d, %v", n, err)
	}
	if got := strings.Join(sessions.deleted, ","); got != "old,b" {
		t.Errorf("deleted = %s", got)
	}
	if strings.Join(forgot, ",") != "111111111111111111,444444444444444444" {
		t.Errorf("forgot = %v", forgot)
	}
}

func TestExploreRequestIDNeverMeetsOtherSessions(t *testing.T) {
	if ExploreRequestID("15", 0) != "discord-explore-15" || ExploreRequestID("15", 2) != "discord-explore-15-2" {
		t.Error("request IDs")
	}
	if ExploreRequestID("15", 0) == ChannelRequestID("15", 0) || ExploreRequestID("15", 0) == OperatorRequestID("15", 0) {
		t.Error("collides")
	}
}

func TestPolicyReadsExplore(t *testing.T) {
	base := `{"userLimit":{"count":1,"window":"1h"},"guildLimit":{"count":1,"window":"1h"},"globalLimit":{"count":1,"window":"1h"},`
	good := base + `"explore":{"agent":"discord-explorer/explorer","limit":{"count":20,"window":"1h"},"concurrency":2,"timeout":"15m","maxSessions":10,"ttl":"168h","maxRepoMB":1024}}`
	c, err := ParsePolicy([]byte(good))
	if err != nil || c.Explore.Agent != explorerRef || c.Explore.MaxRepoKB != 1024*1024 || c.Explore.TTL != 168*time.Hour {
		t.Fatalf("explore = %+v, %v", c.Explore, err)
	}
	for _, bad := range []string{
		`"explore":{"agent":"explorer","limit":{"count":20,"window":"1h"},"concurrency":2,"timeout":"15m","maxSessions":10,"ttl":"168h","maxRepoMB":1024}}`,
		`"explore":{"agent":"a/b","limit":{"count":0,"window":"1h"},"concurrency":2,"timeout":"15m","maxSessions":10,"ttl":"168h","maxRepoMB":1024}}`,
		`"explore":{"agent":"a/b","limit":{"count":20,"window":"1h"},"concurrency":2,"timeout":"2h","maxSessions":10,"ttl":"168h","maxRepoMB":1024}}`,
		`"explore":{"agent":"a/b","limit":{"count":20,"window":"1h"},"concurrency":2,"timeout":"15m","maxSessions":0,"ttl":"168h","maxRepoMB":1024}}`,
	} {
		if _, err := ParsePolicy([]byte(base + bad)); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

func TestAFailedBosunTurnKeepsTheResearchResults(t *testing.T) {
	b, _, _, _ := newExploreBridge("")
	r := ExploreResult{Research: "1501965852042330301", Repo: "a/b", Question: "q", Answer: "x"}
	b.addResult(channel, r)
	b.Agent = &fakeAgent{err: errors.New("down")}
	b.Turn(context.Background(), tagged("1501965852042330302", "and?"))
	agent := &fakeAgent{answer: "ok"}
	b.Agent = agent
	b.Turn(context.Background(), tagged("1501965852042330303", "again"))
	if len(agent.prompts) != 1 || !strings.Contains(agent.prompts[0], `"research_results":[{"research":"1501965852042330301"`) {
		t.Errorf("prompts = %q", agent.prompts)
	}
}

func TestABusyResearchSpendsNoLimit(t *testing.T) {
	b, rep, _, _ := newExploreBridge("")
	b.Config.Explore.Limit = Limit{Count: 1, Window: time.Hour}
	b.Explorer.claim("1501965852042330301")
	l := b.Log
	req := ExploreRequest{Repo: "jomcgi-org/homelab", Question: "q", Research: "1501965852042330301"}
	b.startExplore(context.Background(), l, tagged("1501965852042330302", "x"), req, channel, "", true)
	if posts := rep.all(); len(posts) != 1 || posts[0] != channel+": "+ReplyExploreBusy {
		t.Fatalf("posts = %q", posts)
	}
	b.mu.Lock()
	ok := b.takeExplore(b.now())
	b.mu.Unlock()
	if !ok {
		t.Error("the busy refusal spent the limit")
	}
}
