package requests

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// ErrNoTurn is a session the bridge has not started a turn in.
var ErrNoTurn = errors.New("requests: the bridge knows no turn for this session")

var (
	sessionID = regexp.MustCompile(`^[0-9a-z][0-9a-z-]{0,63}$`)
	snowflake = regexp.MustCompile(`^[0-9]{1,20}$`)
)

// BridgeTurns asks the Discord bridge which turn a session is answering
// (discord.SessionPlaces, GET /sessions/<id>).
type BridgeTurns struct {
	URL  string // REQUESTS_BRIDGE_URL, the bridge Service
	HTTP *http.Client
}

func (b *BridgeTurns) Turn(ctx context.Context, id string) (Origin, error) {
	id = strings.ToLower(id)
	if !sessionID.MatchString(id) {
		return Origin{}, ErrNoTurn
	}
	client := b.HTTP
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(b.URL, "/")+"/sessions/"+url.PathEscape(id), nil)
	if err != nil {
		return Origin{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return Origin{}, fmt.Errorf("requests: bridge: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return Origin{}, ErrNoTurn
	}
	if resp.StatusCode != http.StatusOK {
		return Origin{}, fmt.Errorf("requests: bridge: %s", resp.Status)
	}
	var p struct {
		Server  string `json:"server_id"`
		Channel string `json:"channel_id"`
		Message string `json:"message_id"`
		Author  string `json:"author_id"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<10)).Decode(&p); err != nil {
		return Origin{}, fmt.Errorf("requests: bridge: %w", err)
	}
	o := Origin{Guild: p.Server, Channel: p.Channel, Message: p.Message, Author: p.Author}
	for _, v := range []string{o.Guild, o.Channel, o.Message, o.Author} {
		if !snowflake.MatchString(v) {
			// An older bridge records no message or author.
			return Origin{}, ErrNoTurn
		}
	}
	return o, nil
}
