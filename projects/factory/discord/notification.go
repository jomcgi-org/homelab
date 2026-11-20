// invariant: FACT-13
package discord

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"regexp"

	"github.com/jomcgi/homelab/projects/factory/connector/notify"
)

var channelID = regexp.MustCompile(`^[0-9]{1,20}$`)

func NotificationHandler(channels map[string]string, credential func() (string, error), rest *REST) http.HandlerFunc {
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
		reply := NewReply(n.Level+": "+text, "")
		hash := sha256.Sum256([]byte(channel + "/" + n.Text.Key))
		reply.Nonce = hex.EncodeToString(hash[:12])
		reply.EnforceNonce = true
		if err := rest.Send(r.Context(), channel, reply); err != nil {
			w.WriteHeader(502)
			return
		}
		w.WriteHeader(204)
	}
}
