package notes

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

// ErrNoPlace is a session the bridge has not started a turn in.
var ErrNoPlace = errors.New("notes: the bridge knows no server for this session")

// Place is the server and channel a session belongs to.
type Place struct {
	Server  string `json:"server_id"`
	Channel string `json:"channel_id"`
}

// Places finds the server and channel of a kagent session.
type Places interface {
	Place(ctx context.Context, sessionID string) (Place, error)
}

var sessionID = regexp.MustCompile(`^[0-9a-z][0-9a-z-]{0,63}$`)

// BridgePlaces asks the Discord bridge, which records the server and channel
// of each session when it sends the session a turn
// (discord.SessionPlaces, GET /sessions/<id>).
type BridgePlaces struct {
	URL  string // NOTES_BRIDGE_URL, the bridge Service
	HTTP *http.Client
}

func (b *BridgePlaces) Place(ctx context.Context, id string) (Place, error) {
	id = strings.ToLower(id)
	if !sessionID.MatchString(id) {
		return Place{}, ErrNoPlace
	}
	client := b.HTTP
	if client == nil {
		client = &http.Client{Timeout: 5 * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(b.URL, "/")+"/sessions/"+url.PathEscape(id), nil)
	if err != nil {
		return Place{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return Place{}, fmt.Errorf("notes: bridge: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return Place{}, ErrNoPlace
	}
	if resp.StatusCode != http.StatusOK {
		return Place{}, fmt.Errorf("notes: bridge: %s", resp.Status)
	}
	var p Place
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<10)).Decode(&p); err != nil {
		return Place{}, fmt.Errorf("notes: bridge: %w", err)
	}
	if p.Server == "" {
		return Place{}, ErrNoPlace
	}
	return p, nil
}
