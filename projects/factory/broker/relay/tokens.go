package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
)

// TokenUsage is one Responses API completion. Input includes cached input;
// cached tokens are a subset, never added to input again.
type TokenUsage struct {
	ResponseID                 string
	Input, CachedInput, Output int64
}

// TokenRecorder persists observations independently of dispatch's writer.
// Begin pins attribution before a call can outlive its original turn.
type TokenRecorder interface {
	Begin(context.Context, actorjwt.Identity) (int64, error)
	Complete(context.Context, int64, TokenUsage) error
}

const maxUsageEvent = 32 << 20

// tokenObserver sees a copy of each chunk before the relay writes it. It
// retains one bounded SSE event (or one JSON response), not the stream.
type tokenObserver struct {
	sse                    bool
	line, data             []byte
	lineBytes              int
	lastByte               byte
	jsonDepth              int
	jsonString, jsonEscape bool
	event                  string
	overflow, recorded     bool
	onUsage                func(TokenUsage)
	// sniffed is set once the first non-space body byte chose the format:
	// a Responses JSON body starts with '{', anything else is an event
	// stream, whatever the Content-Type says.
	sniffed bool
	// events counts parsed SSE events and lastType names the last one, for
	// the log line when no usage is observed (names only, never content).
	events   int
	lastType string
}

func (o *tokenObserver) write(p []byte) {
	if o.recorded {
		return
	}
	if !o.sse && !o.sniffed {
		for _, c := range p {
			if c == ' ' || c == '\t' || c == '\r' || c == '\n' {
				continue
			}
			o.sniffed = true
			o.sse = c != '{'
			break
		}
	}
	if !o.sse {
		if len(o.data)+len(p) > maxUsageEvent {
			o.overflow = true
			o.data = nil
		}
		if !o.overflow {
			o.data = append(o.data, p...)
			// Observe a complete JSON object before forwarding its closing
			// byte. A client may decode it without waiting for HTTP EOF.
			for _, c := range p {
				if o.jsonString {
					switch {
					case o.jsonEscape:
						o.jsonEscape = false
					case c == '\\':
						o.jsonEscape = true
					case c == '"':
						o.jsonString = false
					}
					continue
				}
				switch c {
				case '"':
					o.jsonString = true
				case '{', '[':
					o.jsonDepth++
				case '}', ']':
					o.jsonDepth--
					if o.jsonDepth == 0 {
						o.parse(o.data, false)
					}
				}
			}
		}
		return
	}
	for len(p) > 0 {
		i := bytes.IndexByte(p, '\n')
		if i < 0 {
			i = len(p)
		}
		o.lineBytes += i
		if i > 0 {
			o.lastByte = p[i-1]
		}
		if len(o.line)+len(o.data)+i > maxUsageEvent {
			o.overflow = true
			o.line = nil
			o.data = nil
		}
		if !o.overflow {
			o.line = append(o.line, p[:i]...)
		}
		if i == len(p) {
			return
		}
		p = p[i+1:]
		line := bytes.TrimSuffix(o.line, []byte{'\r'})
		// Even an oversized event must end at its own blank line so the
		// next completion can be observed.
		if o.lineBytes == 0 || o.lineBytes == 1 && o.lastByte == '\r' {
			if !o.overflow {
				o.parse(o.data, true)
			}
			o.data, o.event, o.overflow = nil, "", false
		} else if !o.overflow {
			field, value, _ := bytes.Cut(line, []byte{':'})
			value = bytes.TrimPrefix(value, []byte{' '})
			switch string(field) {
			case "data":
				o.data = append(append(o.data, value...), '\n')
			case "event":
				o.event = string(value)
			}
		}
		o.line = nil
		o.lineBytes = 0
		if o.recorded {
			return
		}
	}
}

func (o *tokenObserver) finish() {
	if !o.sse && !o.overflow {
		o.parse(o.data, false)
	}
}

func (o *tokenObserver) parse(data []byte, sse bool) {
	if o.recorded {
		return
	}
	type response struct {
		ID    string `json:"id"`
		Usage *struct {
			Input   *int64 `json:"input_tokens"`
			Output  *int64 `json:"output_tokens"`
			Details struct {
				Cached int64 `json:"cached_tokens"`
			} `json:"input_tokens_details"`
		} `json:"usage"`
	}
	var r response
	if sse {
		if len(data) == 0 && o.event == "" {
			return // a comment or keepalive, not an event
		}
		o.events++
		var event struct {
			Type     string   `json:"type"`
			Response response `json:"response"`
		}
		if json.Unmarshal(data, &event) != nil {
			o.lastType = eventName(o.event)
			return
		}
		if event.Type == "" {
			event.Type = o.event
		}
		o.lastType = eventName(event.Type)
		switch event.Type {
		case "response.completed", "response.incomplete", "response.failed":
		default:
			return
		}
		r = event.Response
	} else if json.Unmarshal(data, &r) != nil {
		return
	}
	if r.ID == "" || r.Usage == nil || r.Usage.Input == nil || r.Usage.Output == nil {
		return
	}
	u := TokenUsage{r.ID, *r.Usage.Input, r.Usage.Details.Cached, *r.Usage.Output}
	if u.Input < 0 || u.Output < 0 || u.CachedInput < 0 || u.CachedInput > u.Input {
		return
	}
	o.recorded = true
	o.onUsage(u)
}

func (rl *Relay) observeTokens(ctx context.Context, id actorjwt.Identity) *tokenObserver {
	if rl.TokenUsage == nil {
		return nil
	}
	// Request cancellation must not discard a completion already read.
	ctx = context.WithoutCancel(ctx)
	start, cancel := context.WithTimeout(ctx, 5*time.Second)
	call, err := rl.TokenUsage.Begin(start, id)
	cancel()
	if err != nil {
		rl.Logger.Warn("codex token accounting start failed")
		rl.Metrics.TokenAccounting.WithLabelValues("start_error").Inc()
		return nil
	}
	if call == 0 {
		return nil
	} // not a kagent session actor
	return &tokenObserver{onUsage: func(u TokenUsage) {
		write, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		if err := rl.TokenUsage.Complete(write, call, u); err != nil {
			rl.Logger.Warn("codex token accounting write failed", "call", call)
			rl.Metrics.TokenAccounting.WithLabelValues("write_error").Inc()
			return
		}
		rl.Metrics.TokenAccounting.WithLabelValues("recorded").Inc()
	}}
}

// eventName keeps an SSE event type for the missing-usage log only when it
// has the shape of a Responses event name, so the log never carries text
// from the body.
var eventNamePattern = regexp.MustCompile(`^[a-z][a-z0-9_.]{0,63}$`)

func eventName(s string) string {
	if eventNamePattern.MatchString(s) {
		return s
	}
	if s == "" {
		return ""
	}
	return "other"
}

func isSSE(contentType string) bool {
	typ, _, _ := strings.Cut(contentType, ";")
	return strings.EqualFold(strings.TrimSpace(typ), "text/event-stream")
}
