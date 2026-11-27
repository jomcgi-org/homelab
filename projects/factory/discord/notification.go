// invariant: FACT-228
// invariant: HOME-228-1
package discord

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/notify"
)

var channelID = regexp.MustCompile(`^[0-9]{1,20}$`)

// maxDedupePages bounds the same-day history scan to 1,000 messages.
const maxDedupePages = 10

// NotificationHandler posts fixed-field notifications. Each post carries a
// "ref" line with its nonce. Before it posts, the handler reads the channel's
// messages from the start of the UTC day and skips a key that Bosun already
// posted. The record is the channel itself, so a same-day replay from a fresh
// connector or bridge process posts nothing (HOME-228-1).
func NotificationHandler(channels map[string]string, credential func() (string, error), rest *REST) http.HandlerFunc {
	var mu sync.Mutex
	var bot string
	return func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		expected, err := credential()
		if !ok || err != nil || expected == "" || user != "factory-connector" || subtle.ConstantTimeCompare([]byte(password), []byte(expected)) != 1 {
			w.WriteHeader(403)
			return
		}
		var n notify.Request
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&n) != nil || decoder.Decode(new(any)) != io.EOF {
			w.WriteHeader(400)
			return
		}
		channel := channels[n.Channel]
		if !channelID.MatchString(channel) {
			w.WriteHeader(403)
			return
		}
		text, err := n.Text.Render()
		if err != nil || (n.Level != "info" && n.Level != "warn" && n.Level != "error") {
			w.WriteHeader(400)
			return
		}
		hash := sha256.Sum256([]byte(channel + "/" + n.Text.Key))
		nonce := hex.EncodeToString(hash[:12])
		ref := "-# ref " + nonce
		reply := NewReply(n.Level+": "+text, "")
		reply.Components = append(reply.Components, ReplyComponent{Type: componentText, Content: ref})
		reply.Nonce = nonce
		reply.EnforceNonce = true
		// postedToday finds the ref in top-level components: no card.
		reply.Accent = 0

		// One check-then-post at a time, so concurrent replays post once.
		mu.Lock()
		defer mu.Unlock()
		if bot == "" {
			me, err := rest.Me(r.Context())
			if err != nil || me.ID == "" {
				w.WriteHeader(502)
				return
			}
			bot = me.ID
		}
		posted, err := postedToday(r.Context(), rest, channel, bot, ref, time.Now().UTC().Truncate(24*time.Hour))
		if err != nil {
			w.WriteHeader(502)
			return
		}
		if posted {
			w.WriteHeader(204)
			return
		}
		if err := rest.Send(r.Context(), channel, reply); err != nil {
			w.WriteHeader(502)
			return
		}
		w.WriteHeader(204)
	}
}

// postedToday reports whether the bot posted ref in channel since dayStart.
func postedToday(ctx context.Context, rest *REST, channel, bot, ref string, dayStart time.Time) (bool, error) {
	before := ""
	for page := 0; page < maxDedupePages; page++ {
		msgs, err := rest.MessagesBefore(ctx, channel, before)
		if err != nil {
			return false, err
		}
		for _, m := range msgs {
			if SentAt(m.Message).Before(dayStart) {
				return false, nil
			}
			if m.Author.ID != bot {
				continue
			}
			for _, c := range m.Components {
				if c.Type == componentText && c.Content == ref {
					return true, nil
				}
			}
		}
		if len(msgs) < 100 {
			return false, nil
		}
		before = msgs[len(msgs)-1].ID
	}
	return false, nil
}
