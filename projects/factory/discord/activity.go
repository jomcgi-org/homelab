package discord

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

const pollInstruction = "When the user requests a poll, reply with only JSON in this A2UI v0.9 updateComponents subset: {\"version\":\"v0.9\",\"updateComponents\":{\"surfaceId\":\"poll\",\"components\":[{\"id\":\"question\",\"component\":\"Text\",\"text\":\"question\"},{\"id\":\"option1\",\"component\":\"Text\",\"text\":\"first choice\"},{\"id\":\"option2\",\"component\":\"Text\",\"text\":\"second choice\"}]}}. Include 2 to 10 choices. Otherwise answer normally.\n"

type pollComponent struct {
	ID        string `json:"id"`
	Component string `json:"component"`
	Text      string `json:"text"`
}

type poll struct {
	guild, channel string
	expires        time.Time
	components     []pollComponent
	votes          map[string]string
	revision       uint64
	changed        chan struct{}
}

type Activities struct {
	rest                 *REST
	clientID             string
	secret               func() (string, error)
	mu                   sync.Mutex
	artifacts, instances map[string]*poll
}

func NewActivities(rest *REST, id string, secret func() (string, error)) *Activities {
	return &Activities{rest: rest, clientID: id, secret: secret, artifacts: map[string]*poll{}, instances: map[string]*poll{}}
}

var (
	optionID  = regexp.MustCompile("^option([1-9]|10)$")
	snowflake = regexp.MustCompile("^[0-9]{1,20}$")
)

func (a *Activities) publish(ctx context.Context, replies Replier, m Message, answer string) bool {
	var doc struct {
		Version          string
		UpdateComponents struct {
			SurfaceID  string
			Components []pollComponent
		}
	}
	if len(answer) > 16384 || json.Unmarshal([]byte(answer), &doc) != nil || doc.Version != "v0.9" || doc.UpdateComponents.SurfaceID != "poll" {
		return false
	}
	p := &poll{guild: m.GuildID, channel: m.ChannelID, expires: time.Now().Add(24 * time.Hour), changed: make(chan struct{}), votes: map[string]string{}}
	seen := map[string]bool{}
	for _, c := range doc.UpdateComponents.Components {
		if c.Component != "Text" || (c.ID != "question" && !optionID.MatchString(c.ID)) {
			continue
		}
		if seen[c.ID] || len(c.Text) > 512 || strings.TrimSpace(c.Text) == "" {
			return false
		}
		seen[c.ID] = true
		p.components = append(p.components, c)
	}
	if !seen["question"] || len(seen) < 3 {
		return false
	}
	a.mu.Lock()
	for _, entries := range []map[string]*poll{a.artifacts, a.instances} {
		for k, v := range entries {
			if time.Now().After(v.expires) {
				delete(entries, k)
			}
		}
	}
	a.artifacts[m.ID] = p
	a.mu.Unlock()
	r := NewReply("Open the poll to vote.", m.ID)
	r.Components = append(r.Components, TextDisplay{Type: 1, Components: []Button{{Type: 2, Style: 1, Label: "Open", CustomID: "poll/" + m.ID}}})
	return replies.Send(ctx, m.ChannelID, r) == nil
}

type Interaction struct {
	ID        string
	Token     string
	Type      int
	GuildID   string `json:"guild_id"`
	ChannelID string `json:"channel_id"`
	Data      struct {
		CustomID string `json:"custom_id"`
	}
}

func (a *Activities) Launch(ctx context.Context, i Interaction) {
	id, ok := strings.CutPrefix(i.Data.CustomID, "poll/")
	if !ok || i.Type != 3 || !snowflake.MatchString(i.ID) {
		return
	}
	a.mu.Lock()
	p := a.artifacts[id]
	a.mu.Unlock()
	if p == nil || p.guild != i.GuildID || p.channel != i.ChannelID || time.Now().After(p.expires) {
		return
	}
	var callback struct {
		Resource struct {
			ActivityInstance struct{ ID string } `json:"activity_instance"`
		}
	}
	err := a.rest.do(ctx, "POST", "/interactions/"+i.ID+"/"+url.PathEscape(i.Token)+"/callback?with_response=true", map[string]int{"type": 12}, &callback)
	if instance := callback.Resource.ActivityInstance.ID; err == nil && instance != "" {
		a.mu.Lock()
		defer a.mu.Unlock()
		if existing := a.instances[instance]; existing == nil {
			a.instances[instance] = p
		}
	}
}

func (a *Activities) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	if r.URL.Path == "/activity/token" && r.Method == "POST" {
		a.exchange(w, r)
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/activity/instances/")
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") || len(auth) <= 7 {
		w.WriteHeader(401)
		return
	}
	a.mu.Lock()
	p := a.instances[id]
	a.mu.Unlock()
	if p == nil || time.Now().After(p.expires) {
		w.WriteHeader(404)
		return
	}
	user, status := a.member(r, p, auth)
	if status != 200 {
		w.WriteHeader(status)
		return
	}
	var choice string
	if r.Method == "POST" {
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 256)).Decode(&choice) != nil || !optionID.MatchString(choice) {
			w.WriteHeader(400)
			return
		}
	} else if r.Method != "GET" {
		w.WriteHeader(405)
		return
	}
	a.mu.Lock()
	if choice != "" {
		found := false
		for _, c := range p.components {
			if c.ID == choice {
				found = true
			}
		}
		if !found {
			a.mu.Unlock()
			w.WriteHeader(400)
			return
		}
		if p.votes[user] != choice {
			p.votes[user] = choice
			p.revision++
			p.signal()
		}
	}
	if r.Method == "GET" && r.Header.Get("Accept") == "application/x-ndjson" {
		w.Header().Set("Content-Type", "application/x-ndjson")
		for {
			changed := p.changed
			snapshot := p.snapshot()
			a.mu.Unlock()
			if json.NewEncoder(w).Encode(snapshot) != nil || http.NewResponseController(w).Flush() != nil {
				return
			}
			select {
			case <-r.Context().Done():
				return
			case <-changed:
			case <-time.After(25 * time.Second):
			}
			if time.Now().After(p.expires) {
				return
			}
			if _, status := a.member(r, p, auth); status != 200 {
				return
			}
			a.mu.Lock()
		}
	}
	snapshot := p.snapshot()
	a.mu.Unlock()
	json.NewEncoder(w).Encode(snapshot)
}

func (p *poll) signal() { close(p.changed); p.changed = make(chan struct{}) }
func (p *poll) counts() map[string]int {
	counts := map[string]int{}
	for _, v := range p.votes {
		counts[v]++
	}
	return counts
}

func (p *poll) snapshot() map[string]any {
	counts := p.counts()
	components := p.components
	if summary := p.summary(counts); summary != "" {
		components = append(append([]pollComponent{}, components...), pollComponent{ID: "summary", Component: "Text", Text: summary})
	}
	return map[string]any{"components": components, "counts": counts}
}

// invariant: HOME-170-3
// summary states the vote counts. It comes from the counts alone, with no
// agent call, so no model-written poll text reaches an agent prompt.
func (p *poll) summary(counts map[string]int) string {
	total, top := 0, 0
	for _, n := range counts {
		total += n
		top = max(top, n)
	}
	if total == 0 {
		return ""
	}
	var leaders []string
	for _, c := range p.components {
		if c.ID != "question" && counts[c.ID] == top {
			leaders = append(leaders, c.Text)
		}
	}
	votes := "votes"
	if total == 1 {
		votes = "vote"
	}
	if len(leaders) == 1 {
		return fmt.Sprintf("%s leads with %d of %d %s.", leaders[0], top, total, votes)
	}
	return fmt.Sprintf("Tied at %d of %d %s each: %s.", top, total, votes, strings.Join(leaders, ", "))
}

// invariant: HOME-170-1
func (a *Activities) member(r *http.Request, p *poll, auth string) (string, int) {
	base := a.rest.Base
	if base == "" {
		base = APIBase
	}
	req, err := http.NewRequestWithContext(r.Context(), "GET", base+"/users/@me/guilds/"+p.guild+"/member", nil)
	if err != nil {
		return "", 403
	}
	req.Header.Set("Authorization", auth)
	client := a.rest.HTTP
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", 503
	}
	defer resp.Body.Close()
	var member struct{ User User }
	if resp.StatusCode != 200 || json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&member) != nil || !snowflake.MatchString(member.User.ID) {
		return "", 403
	}
	return member.User.ID, 200
}

func (a *Activities) exchange(w http.ResponseWriter, r *http.Request) {
	var code string
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 2048)).Decode(&code) != nil || code == "" {
		w.WriteHeader(400)
		return
	}
	secret, err := a.secret()
	if err != nil {
		w.WriteHeader(503)
		return
	}
	form := url.Values{"client_id": {a.clientID}, "client_secret": {secret}, "grant_type": {"authorization_code"}, "code": {code}}
	req, err := http.NewRequestWithContext(r.Context(), "POST", APIBase+"/oauth2/token", strings.NewReader(form.Encode()))
	if err != nil {
		w.WriteHeader(500)
		return
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	client := http.Client{Timeout: 5 * time.Second}
	res, err := client.Do(req)
	if err != nil {
		w.WriteHeader(503)
		return
	}
	defer res.Body.Close()
	var token struct {
		AccessToken string `json:"access_token"`
	}
	if res.StatusCode != 200 || json.NewDecoder(io.LimitReader(res.Body, 16384)).Decode(&token) != nil || token.AccessToken == "" {
		w.WriteHeader(401)
		return
	}
	json.NewEncoder(w).Encode(token)
}
