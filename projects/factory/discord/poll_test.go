package discord

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestParsePollAcceptsOnlyDiscordSizedPolls(t *testing.T) {
	long := func(n int) string { return strings.Repeat("a", n) }
	answers := func(n int) string { return "[" + strings.TrimSuffix(strings.Repeat(`"x",`, n), ",") + "]" }
	cases := []struct {
		name string
		spec string
		ok   bool
	}{
		{"minimal", `{"question":"Saturday?","answers":["Yes","No"]}`, true},
		{"objects and emoji", `{"question":"Saturday?","answers":[{"text":"Morning","emoji":"🌅"},{"text":"Evening"}],"duration_hours":48,"allow_multiselect":true}`, true},
		{"keycap and flag emoji", `{"question":"q","answers":[{"text":"a","emoji":"1️⃣"},{"text":"b","emoji":"🇬🇧"}]}`, true},
		{"longest sizes", `{"question":"` + long(300) + `","answers":["` + long(55) + `","b"],"duration_hours":768}`, true},
		{"ten answers", `{"question":"q","answers":` + answers(10) + `}`, true},
		{"one answer", `{"question":"q","answers":["only"]}`, false},
		{"eleven answers", `{"question":"q","answers":` + answers(11) + `}`, false},
		{"long question", `{"question":"` + long(301) + `","answers":["a","b"]}`, false},
		{"long answer", `{"question":"q","answers":["` + long(56) + `","b"]}`, false},
		{"empty question", `{"question":"  ","answers":["a","b"]}`, false},
		{"empty answer", `{"question":"q","answers":["a",""]}`, false},
		{"zero hours", `{"question":"q","answers":["a","b"],"duration_hours":0}`, false},
		{"too many hours", `{"question":"q","answers":["a","b"],"duration_hours":769}`, false},
		{"unknown field", `{"question":"q","answers":["a","b"],"layout":2}`, false},
		{"unknown answer field", `{"question":"q","answers":[{"text":"a","id":"1"},"b"]}`, false},
		{"custom emoji", `{"question":"q","answers":[{"text":"a","emoji":"<:x:123>"},"b"]}`, false},
		{"digit emoji", `{"question":"q","answers":[{"text":"a","emoji":"1"},"b"]}`, false},
		{"punctuation emoji", `{"question":"q","answers":[{"text":"a","emoji":"#-"},"b"]}`, false},
		{"word emoji", `{"question":"q","answers":[{"text":"a","emoji":"smile"},"b"]}`, false},
		{"trailing data", `{"question":"q","answers":["a","b"]} {}`, false},
		{"not json", `question: q`, false},
		{"multi-line question", `{"question":"q\nr","answers":["a","b"]}`, false},
	}
	for _, c := range cases {
		p, err := ParsePoll(c.spec)
		if (err == nil) != c.ok {
			t.Errorf("%s: err = %v, want ok %v", c.name, err, c.ok)
		}
		if c.ok && p.Duration < 1 {
			t.Errorf("%s: duration %d", c.name, p.Duration)
		}
	}
	p, _ := ParsePoll(`{"question":" Saturday? ","answers":["Yes","No"]}`)
	if p.Duration != 24 || p.AllowMultiselect || p.Question.Text != "Saturday?" || p.LayoutType != 1 {
		t.Errorf("defaults = %+v", p)
	}
}

func TestPollsTakesTheFirstValidBlockOnly(t *testing.T) {
	answer := "Here:\n```poll\n{\"question\":\"bad\",\"answers\":[\"a\"]}\n```\n" +
		"```poll\n{\"question\":\"Saturday?\",\"answers\":[\"Yes\",\"No\"]}\n```\n" +
		"```poll\n{\"question\":\"Sunday?\",\"answers\":[\"Yes\",\"No\"]}\n```\nend"
	rest, p := Polls(answer)
	if p == nil || p.Question.Text != "Saturday?" {
		t.Fatalf("poll = %+v", p)
	}
	text, memo := PlacePoll(rest, rest, p)
	if !strings.Contains(text, `"question":"bad"`) || !strings.Contains(text, "Sunday?") || strings.Contains(text, "Saturday?") {
		t.Errorf("text = %q", text)
	}
	if !strings.Contains(memo, "[poll: Saturday?]") || strings.Contains(memo, `"question":"Saturday?"`) {
		t.Errorf("memo = %q", memo)
	}
	if rest, p := Polls("no poll " + pollMark); p != nil || rest != "no poll " {
		t.Errorf("an answer without a poll = %q, %+v", rest, p)
	}
}

func TestAPollMessageBodyHasNoComponentsV2Flag(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/channels/"+channel+"/messages" || r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("request = %s %s", r.URL.Path, r.Header.Get("Content-Type"))
		}
		data, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(data, &got)
		w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	p, err := ParsePoll(`{"question":"Saturday?","answers":[{"text":"Morning","emoji":"🌅"},"Evening"],"duration_hours":12}`)
	if err != nil {
		t.Fatal(err)
	}
	rest := &REST{Base: srv.URL, Token: func() (string, error) { return "t", nil }}
	if err := rest.Send(context.Background(), channel, NewPollReply(p, "9")); err != nil {
		t.Fatal(err)
	}
	want := `{"allowed_mentions":{"parse":[],"replied_user":false},"flags":0,` +
		`"message_reference":{"fail_if_not_exists":false,"message_id":"9"},` +
		`"poll":{"allow_multiselect":false,"answers":[{"poll_media":{"emoji":{"name":"🌅"},"text":"Morning"}},{"poll_media":{"text":"Evening"}}],` +
		`"duration":12,"layout_type":1,"question":{"text":"Saturday?"}}}`
	if raw, _ := json.Marshal(got); string(raw) != want {
		t.Errorf("body = %s\nwant   %s", raw, want)
	}
}

func TestATurnPostsTheTextThenThePollAndStoresItsName(t *testing.T) {
	agent := &fakeAgent{answer: "Here you go:\n```poll\n{\"question\":\"Saturday?\",\"answers\":[\"Hike\",\"Pub\"]}\n```"}
	b, mem, rep := newBridge(agent)
	b.Turn(context.Background(), tagged("1", "make a poll for us about Saturday"))
	if len(rep.sent) != 2 {
		t.Fatalf("sent %d messages, want text then poll: %+v", len(rep.sent), rep.sent)
	}
	if rep.sent[0].Poll != nil || rep.sent[0].Components[0].Content != "Here you go:" {
		t.Errorf("first message = %+v", rep.sent[0])
	}
	if p := rep.sent[1]; p.Poll == nil || p.Poll.Question.Text != "Saturday?" || p.Flags != 0 || len(p.Components) != 0 {
		t.Errorf("second message = %+v", p)
	}
	if len(mem.retained) != 1 || !strings.HasSuffix(mem.retained[0], "Here you go:\n[poll: Saturday?]") || strings.Contains(mem.retained[0], "answers") {
		t.Errorf("retained = %v", mem.retained)
	}

	// A poll alone posts no empty text message.
	agent.answer = "```poll\n{\"question\":\"Sunday?\",\"answers\":[\"Hike\",\"Pub\"]}\n```"
	rep.sent = nil
	b.Turn(context.Background(), tagged("2", "and Sunday"))
	if len(rep.sent) != 1 || rep.sent[0].Poll == nil {
		t.Errorf("poll-only answer sent %+v", rep.sent)
	}
}

type failingPolls struct{ fakeReplies }

func (f *failingPolls) Send(ctx context.Context, c string, r Reply) error {
	if r.Poll != nil {
		return io.ErrUnexpectedEOF
	}
	return f.fakeReplies.Send(ctx, c, r)
}

func TestARefusedPollIsNotNamedInMemory(t *testing.T) {
	agent := &fakeAgent{answer: "Here:\n```poll\n{\"question\":\"Saturday?\",\"answers\":[\"Hike\",\"Pub\"]}\n```"}
	b, mem, _ := newBridge(agent)
	b.Replies = &failingPolls{}
	b.Turn(context.Background(), tagged("1", "poll"))
	if len(mem.retained) != 1 || strings.Contains(mem.retained[0], "[poll:") || !strings.HasSuffix(mem.retained[0], "Here:") {
		t.Errorf("retained = %v", mem.retained)
	}
}

func TestARefusedPollAloneRepliesAFixedText(t *testing.T) {
	agent := &fakeAgent{answer: "```poll\n{\"question\":\"Saturday?\",\"answers\":[\"Hike\",\"Pub\"]}\n```"}
	b, _, _ := newBridge(agent)
	rep := &failingPolls{}
	b.Replies = rep
	b.Turn(context.Background(), tagged("1", "poll"))
	if len(rep.sent) != 1 || rep.sent[0].Components[0].Content != ReplyPollFailed {
		t.Errorf("sent %+v", rep.sent)
	}
}

func TestAFailedTurnPostsNoPoll(t *testing.T) {
	agent := &fakeAgent{answer: "```poll\n{\"question\":\"q\",\"answers\":[\"a\",\"b\"]}\n```", err: io.ErrUnexpectedEOF}
	b, _, rep := newBridge(agent)
	b.Turn(context.Background(), tagged("1", "poll"))
	if len(rep.sent) != 1 || rep.sent[0].Poll != nil {
		t.Errorf("sent %+v", rep.sent)
	}
}
