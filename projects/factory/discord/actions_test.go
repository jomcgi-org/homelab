package discord

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestParseActionsIsStrict(t *testing.T) {
	for _, spec := range []string{`{"emoji":["👍"]}`, `{"emoji":["👍","🎉","1️⃣"]}`} {
		if _, err := ParseReact(spec); err != nil {
			t.Errorf("ParseReact(%s): %v", spec, err)
		}
	}
	for _, spec := range []string{
		`{"emoji":[]}`, `{"emoji":["👍","🎉","🍕","🌅"]}`, `{"emoji":["👍","👍"]}`,
		`{"emoji":["<:x:123>"]}`, `{"emoji":["ok"]}`, `{"emoji":["👍"],"x":1}`, `{"emoji":["👍"]} {}`, `["👍"]`,
	} {
		if _, err := ParseReact(spec); err == nil {
			t.Errorf("ParseReact(%s) accepted", spec)
		}
	}
	if name, err := ParseThread(`{"name":"  Trip plans "}`); err != nil || name != "Trip plans" {
		t.Errorf("ParseThread = %q, %v", name, err)
	}
	for _, spec := range []string{`{"name":""}`, `{"name":"a\nb"}`, `{"name":"` + strings.Repeat("é", 101) + `"}`, `{"title":"x"}`} {
		if _, err := ParseThread(spec); err == nil {
			t.Errorf("ParseThread(%s) accepted", spec)
		}
	}
	for _, spec := range []string{"", " \n", "{}"} {
		if err := ParsePin(spec); err != nil {
			t.Errorf("ParsePin(%q): %v", spec, err)
		}
	}
	for _, spec := range []string{`{"id":"1"}`, `[]`, `yes`} {
		if ParsePin(spec) == nil {
			t.Errorf("ParsePin(%q) accepted", spec)
		}
	}
}

func TestTakeActionsTakesOneOfEachKind(t *testing.T) {
	answer := "Done.\n```react\n{\"emoji\":[\"👍\"]}\n```\n```react\n{\"emoji\":[\"🎉\"]}\n```\n" +
		"```thread\n{\"name\":\"\"}\n```\n```thread\n{\"name\":\"Plans\"}\n```\n```pin\n```"
	text, a := TakeActions(answer)
	if strings.Join(a.React, ",") != "👍" || a.Thread != "Plans" || !a.Pin {
		t.Errorf("actions = %+v", a)
	}
	// The second react and the invalid thread stay as text.
	want := "Done.\n\n```react\n{\"emoji\":[\"🎉\"]}\n```\n```thread\n{\"name\":\"\"}\n```"
	if text != want {
		t.Errorf("text = %q\nwant   %q", text, want)
	}
	if got := a.Tags(); got != "[thread: Plans] [reacted: 👍] [pinned]" {
		t.Errorf("tags = %q", got)
	}
}

func TestRESTActionCalls(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		mu.Lock()
		calls = append(calls, r.Method+" "+r.URL.EscapedPath()+" "+string(data))
		mu.Unlock()
		switch {
		case strings.HasSuffix(r.URL.Path, "/threads"):
			w.Write([]byte(`{"id":"777"}`))
		case strings.HasSuffix(r.URL.Path, "/messages"):
			w.Write([]byte(`{"id":"555"}`))
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer srv.Close()
	rest := &REST{Base: srv.URL, Token: func() (string, error) { return "t", nil }}
	ctx := context.Background()
	if err := rest.React(ctx, "5", "9", "👍"); err != nil {
		t.Fatal(err)
	}
	if id, err := rest.StartThread(ctx, "5", "9", "Plans"); err != nil || id != "777" {
		t.Fatalf("StartThread = %q, %v", id, err)
	}
	if id, err := rest.Post(ctx, "777", NewReply("hi", "")); err != nil || id != "555" {
		t.Fatalf("Post = %q, %v", id, err)
	}
	if err := rest.Pin(ctx, "777", "555"); err != nil {
		t.Fatal(err)
	}
	want := []string{
		"PUT /channels/5/messages/9/reactions/%F0%9F%91%8D/@me ",
		`POST /channels/5/messages/9/threads {"name":"Plans","auto_archive_duration":1440}`,
		`POST /channels/777/messages {"flags":32768,"components":[{"type":17,"accent_color":2783446,"components":[{"type":10,"content":"hi"}]}],"allowed_mentions":{"parse":[],"replied_user":false}}`,
		"PUT /channels/777/messages/pins/555 ",
	}
	if strings.Join(calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s\nwant:\n%s", strings.Join(calls, "\n"), strings.Join(want, "\n"))
	}
}

// actingReplies records message actions. fail names actions that Discord
// refuses.
type actingReplies struct {
	fakeReplies
	channels []string
	actions  []string
	fail     map[string]bool
}

func (f *actingReplies) Send(ctx context.Context, c string, r Reply) error {
	if r.Poll != nil && f.fail["poll"] {
		return io.ErrUnexpectedEOF
	}
	f.channels = append(f.channels, c)
	return f.fakeReplies.Send(ctx, c, r)
}

func (f *actingReplies) Post(ctx context.Context, c string, r Reply) (string, error) {
	if f.fail["post:"+c] {
		return "", io.ErrUnexpectedEOF
	}
	if err := f.Send(ctx, c, r); err != nil {
		return "", err
	}
	return fmt.Sprintf("r%d", len(f.sent)), nil
}

func (f *actingReplies) React(_ context.Context, c, m, e string) error {
	if f.fail["react:"+e] {
		return &HTTPError{Status: http.StatusForbidden}
	}
	f.actions = append(f.actions, "react "+c+"/"+m+" "+e)
	return nil
}

func (f *actingReplies) StartThread(_ context.Context, c, m, name string) (string, error) {
	if f.fail["thread"] {
		return "", &HTTPError{Status: http.StatusForbidden}
	}
	f.actions = append(f.actions, "thread "+c+"/"+m+" "+name)
	return "thread-1", nil
}

func (f *actingReplies) Pin(_ context.Context, c, m string) error {
	if f.fail["pin"] {
		return &HTTPError{Status: http.StatusForbidden}
	}
	f.actions = append(f.actions, "pin "+c+"/"+m)
	return nil
}

const allActions = "Sure, a thread for it.\n```thread\n{\"name\":\"Saturday plans\"}\n```\n" +
	"```react\n{\"emoji\":[\"👍\",\"🎉\"]}\n```\n```pin\n{}\n```"

func TestATurnRepliesInTheThreadThenReactsAndPins(t *testing.T) {
	b, mem, _ := newBridge(&fakeAgent{answer: allActions})
	rep := &actingReplies{}
	b.Replies = rep
	b.Turn(context.Background(), tagged("1", "plan Saturday"))
	if len(rep.sent) != 1 || rep.channels[0] != "thread-1" || rep.sent[0].MessageReference != nil ||
		rep.sent[0].Components[0].Content != "Sure, a thread for it." || len(rep.sent[0].AllowedMentions.Parse) != 0 {
		t.Fatalf("sent %+v in %v", rep.sent, rep.channels)
	}
	want := "thread " + channel + "/1 Saturday plans,react " + channel + "/1 👍,react " + channel + "/1 🎉,pin thread-1/r1"
	if got := strings.Join(rep.actions, ","); got != want {
		t.Errorf("actions = %s\nwant      %s", got, want)
	}
	if len(mem.retained) != 1 || !strings.HasSuffix(mem.retained[0], "Sure, a thread for it.\n[thread: Saturday plans] [reacted: 👍 🎉] [pinned]") {
		t.Errorf("retained = %v", mem.retained)
	}
}

func TestRefusedActionsFailSoftly(t *testing.T) {
	b, mem, _ := newBridge(&fakeAgent{answer: allActions})
	rep := &actingReplies{fail: map[string]bool{"thread": true, "react:🎉": true, "pin": true}}
	b.Replies = rep
	b.Turn(context.Background(), tagged("1", "plan Saturday"))
	// No thread: the reply goes to the channel as usual.
	if len(rep.sent) != 1 || rep.channels[0] != channel || rep.sent[0].MessageReference.MessageID != "1" {
		t.Fatalf("sent %+v in %v", rep.sent, rep.channels)
	}
	if len(mem.retained) != 1 || !strings.HasSuffix(mem.retained[0], "Sure, a thread for it.\n[reacted: 👍]") {
		t.Errorf("retained = %v", mem.retained)
	}

	// A reply the thread refuses goes to the channel.
	rep = &actingReplies{fail: map[string]bool{"post:thread-1": true}}
	b.Replies = rep
	b.Turn(context.Background(), tagged("2", "again"))
	if len(rep.sent) != 1 || rep.channels[0] != channel || !strings.Contains(strings.Join(rep.actions, ","), "pin "+channel+"/r1") {
		t.Errorf("sent %+v in %v, actions %v", rep.sent, rep.channels, rep.actions)
	}
}

func TestAnActionOnlyAnswer(t *testing.T) {
	agent := &fakeAgent{answer: "```react\n{\"emoji\":[\"👍\"]}\n```"}
	b, mem, _ := newBridge(agent)
	rep := &actingReplies{}
	b.Replies = rep
	b.Turn(context.Background(), tagged("1", "thumbs up this"))
	if len(rep.sent) != 0 || len(rep.actions) != 1 || !strings.HasSuffix(mem.retained[0], "Bosun (2026-11-17T12:05:00Z): [reacted: 👍]") {
		t.Errorf("sent %+v, actions %v, retained %v", rep.sent, rep.actions, mem.retained)
	}

	// When Discord refuses all of it, a fixed reply says so.
	rep = &actingReplies{fail: map[string]bool{"react:👍": true}}
	b.Replies = rep
	b.Turn(context.Background(), tagged("2", "again"))
	if len(rep.sent) != 1 || rep.sent[0].Components[0].Content != ReplyActionFailed {
		t.Errorf("sent %+v", rep.sent)
	}
}

func TestWithoutAnActorBlocksStayText(t *testing.T) {
	b, _, rep := newBridge(&fakeAgent{answer: "ok\n```pin\n```"})
	b.Turn(context.Background(), tagged("1", "pin it"))
	if len(rep.sent) != 1 || !strings.Contains(rep.sent[0].Components[0].Content, "```pin") {
		t.Errorf("sent %+v", rep.sent)
	}
}
