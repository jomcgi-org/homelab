package discord

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestParseEventIsStrict(t *testing.T) {
	e, err := ParseEvent(`{"name":" Pub quiz ","start":"2026-12-04T19:30:00+00:00","location":"The Crown","description":"Teams of four"}`)
	if err != nil || e.Name != "Pub quiz" || e.Location != "The Crown" || e.Description != "Teams of four" ||
		!e.End.Equal(e.Start.Add(time.Hour)) || e.Start.Unix() != 1796412600 {
		t.Fatalf("ParseEvent = %+v, %v", e, err)
	}
	if e, err := ParseEvent(`{"name":"Games","start":"2026-12-04T20:00:00+01:00","end":"2026-12-04T23:00:00+01:00","channel":"#Lounge"}`); err != nil || e.Channel != "Lounge" {
		t.Fatalf("ParseEvent voice = %+v, %v", e, err)
	}
	for _, spec := range []string{
		`{"name":"x","start":"2026-12-04T19:30:00","location":"a"}`,                                 // no offset
		`{"name":"x","start":"4 Dec","location":"a"}`,                                               // not RFC 3339
		`{"name":"x","start":"2026-12-04T19:30:00Z"}`,                                               // no place
		`{"name":"x","start":"2026-12-04T19:30:00Z","location":"a","channel":"b"}`,                  // two places
		`{"name":"","start":"2026-12-04T19:30:00Z","location":"a"}`,                                 // no name
		`{"name":"a\nb","start":"2026-12-04T19:30:00Z","location":"a"}`,                             // two lines
		`{"name":"` + strings.Repeat("é", 101) + `","start":"2026-12-04T19:30:00Z","location":"a"}`, // too long
		`{"name":"x","start":"2026-12-04T19:30:00Z","location":"` + strings.Repeat("a", 101) + `"}`,
		`{"name":"x","start":"2026-12-04T19:30:00Z","location":"a","description":"` + strings.Repeat("a", 1001) + `"}`,
		`{"name":"x","start":"2026-12-04T19:30:00Z","end":"2026-12-04T19:00:00Z","location":"a"}`, // ends first
		`{"name":"x","start":"2026-12-04T19:30:00Z","end":"2026-12-14T19:00:00Z","location":"a"}`, // too long
		`{"name":"@everyone party","start":"2026-12-04T19:30:00Z","location":"a"}`,
		`{"name":"x","start":"2026-12-04T19:30:00Z","location":"a","description":"ask <@123>"}`,
		`{"name":"x","start":"2026-12-04T19:30:00Z","location":"a","x":1}`,
	} {
		if _, err := ParseEvent(spec); err == nil {
			t.Errorf("ParseEvent(%s) accepted", spec)
		}
	}
	now := time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC)
	for start, ok := range map[string]bool{"2026-11-27T11:00:00Z": false, "2026-11-28T09:00:00Z": true, "2027-12-01T09:00:00Z": false} {
		at, _ := time.Parse(time.RFC3339, start)
		if err := (EventRequest{Start: at}).Check(now); (err == nil) != ok {
			t.Errorf("Check(%s) = %v", start, err)
		}
	}
}

func TestCreateEventPostsAServerOnlyEvent(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		calls = append(calls, r.Method+" "+r.URL.Path+" "+string(data))
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`[{"id":"c1","name":"lounge","type":0},{"id":"c2","name":"Lounge","type":2}]`))
			return
		}
		_, _ = w.Write([]byte(`{"id":"e1"}`))
	}))
	defer srv.Close()
	rest := &REST{Base: srv.URL, Token: func() (string, error) { return "t", nil }}
	ctx := context.Background()
	ext, _ := ParseEvent(`{"name":"Pub quiz","start":"2026-12-04T19:30:00+01:00","location":"The Crown","description":"Teams"}`)
	voice, _ := ParseEvent(`{"name":"Games","start":"2026-12-04T20:00:00Z","channel":"lounge"}`)
	for _, e := range []EventRequest{ext, voice} {
		if id, err := rest.CreateEvent(ctx, "g1", e); err != nil || id != "e1" {
			t.Fatalf("CreateEvent = %q, %v", id, err)
		}
	}
	want := []string{
		`POST /guilds/g1/scheduled-events {"name":"Pub quiz","privacy_level":2,"scheduled_start_time":"2026-12-04T18:30:00Z","scheduled_end_time":"2026-12-04T19:30:00Z","description":"Teams","entity_type":3,"entity_metadata":{"location":"The Crown"}}`,
		"GET /guilds/g1/channels ",
		`POST /guilds/g1/scheduled-events {"name":"Games","privacy_level":2,"scheduled_start_time":"2026-12-04T20:00:00Z","entity_type":2,"channel_id":"c2"}`,
	}
	if strings.Join(calls, "\n") != strings.Join(want, "\n") {
		t.Errorf("calls:\n%s\nwant:\n%s", strings.Join(calls, "\n"), strings.Join(want, "\n"))
	}
	missing, _ := ParseEvent(`{"name":"Games","start":"2026-12-04T20:00:00Z","channel":"stage"}`)
	if _, err := rest.CreateEvent(ctx, "g1", missing); err != errNoVoiceChannel {
		t.Errorf("missing channel: %v", err)
	}
}

// eventReplies is an actingReplies that creates events.
type eventReplies struct {
	actingReplies
	events []EventRequest
	err    error
}

func (f *eventReplies) CreateEvent(_ context.Context, g string, e EventRequest) (string, error) {
	if f.err != nil {
		return "", f.err
	}
	f.events = append(f.events, e)
	return "e1", nil
}

const eventAnswer = "Booked.\n```event\n{\"name\":\"Pub quiz\",\"start\":\"2026-11-20T19:30:00Z\",\"location\":\"The Crown\"}\n```\n" +
	"```event\n{\"name\":\"Second\",\"start\":\"2026-11-21T19:30:00Z\",\"location\":\"The Crown\"}\n```"

func TestATurnCreatesOneEventAndLinksIt(t *testing.T) {
	b, mem, _ := newBridge(&fakeAgent{answer: eventAnswer})
	rep := &eventReplies{}
	b.Replies = rep
	b.Turn(context.Background(), tagged("1", "book the quiz"))
	if len(rep.events) != 1 || rep.events[0].Name != "Pub quiz" {
		t.Fatalf("events = %+v", rep.events)
	}
	got := rep.sent[0].Components[0].Content
	if !strings.HasPrefix(got, "Booked.\n\n```event") || !strings.HasSuffix(got, "📅 **Pub quiz** · <t:1795203000:F> (<t:1795203000:R>)\nhttps://discord.com/events/"+guild+"/e1") ||
		len(rep.sent[0].AllowedMentions.Parse) != 0 {
		t.Errorf("reply = %q", got)
	}
	if !strings.HasSuffix(mem.retained[0], "[event: Pub quiz, 2026-11-20T19:30:00Z]") {
		t.Errorf("retained = %v", mem.retained)
	}
}

func TestARefusedEventFailsSoftly(t *testing.T) {
	for name, c := range map[string]struct {
		answer string
		err    error
		want   string
	}{
		"past":       {"```event\n{\"name\":\"Old\",\"start\":\"2026-11-01T19:30:00Z\",\"location\":\"x\"}\n```", nil, "its start is in the past"},
		"permission": {eventAnswer, &HTTPError{Status: http.StatusForbidden}, "I lack the Create Events permission here"},
	} {
		b, mem, _ := newBridge(&fakeAgent{answer: c.answer})
		rep := &eventReplies{err: c.err}
		b.Replies = rep
		b.Turn(context.Background(), tagged("1", "book it"))
		got := rep.sent[0].Components[0].Content
		if !strings.HasSuffix(got, "-# I couldn't create the event: "+c.want+".") || strings.Contains(mem.retained[0], "[event:") {
			t.Errorf("%s: reply = %q, retained %v", name, got, mem.retained)
		}
	}
}
