package discord

import (
	"encoding/json"
	"testing"
)

// Guild and channel events keep the cache current; an outage keeps it.
func TestPlacesFollowGuildAndChannelEvents(t *testing.T) {
	p := &Places{}
	apply := func(event, data string) { p.Apply(event, json.RawMessage(data)) }
	apply("GUILD_CREATE", `{"id":"g","name":"lab","description":"old","member_count":4,"channels":[{"id":"c","name":"general","type":0}]}`)
	apply("GUILD_UPDATE", `{"id":"g","name":"homelab","description":null}`)
	apply("THREAD_CREATE", `{"id":"t","guild_id":"g","name":"plan","type":11,"parent_id":"c"}`)
	apply("CHANNEL_UPDATE", `{"id":"c","guild_id":"g","name":"lobby","type":0}`)
	apply("CHANNEL_CREATE", `{"id":"x","guild_id":"other","name":"ignored","type":0}`)
	want := Place{Server: Server{Name: "homelab", MemberCount: 4}, Channel: ChannelPlace{Name: "plan", Parent: "lobby"}}
	if got := p.Lookup("g", "t"); got != want {
		t.Errorf("thread place = %+v, want %+v", got, want)
	}
	apply("THREAD_DELETE", `{"id":"t","guild_id":"g","type":11,"parent_id":"c"}`)
	if got := p.Lookup("g", "t").Channel; got != (ChannelPlace{}) {
		t.Errorf("deleted thread = %+v", got)
	}
	apply("GUILD_DELETE", `{"id":"g","unavailable":true}`)
	if p.Lookup("g", "c").Server.Name != "homelab" {
		t.Error("an outage dropped the server")
	}
	apply("GUILD_DELETE", `{"id":"g"}`)
	if got := p.Lookup("g", "c"); got != (Place{}) {
		t.Errorf("a removed server is still cached: %+v", got)
	}
}
