package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

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
	providerFailed           bool
	traceOnly                bool
	claudeSpan               trace.Span
	sse                      bool
	line, data               []byte
	claudeKey, claudeCapture bool
	lineBytes                int
	lastByte                 byte
	jsonDepth                int
	jsonString, jsonEscape   bool
	event                    string
	overflow, recorded       bool
	onUsage                  func(TokenUsage)
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
	limit := maxUsageEvent
	if o.claudeSpan != nil {
		// Claude observes usage for tracing on up to 64 concurrent streams.
		limit = 64 << 10
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
	if !o.sse && o.claudeSpan != nil {
		o.writeClaudeJSON(p, limit)
		return
	}
	if !o.sse {
		if len(o.data)+len(p) > limit {
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
		if len(o.line)+len(o.data)+i > limit {
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
			if o.event == "error" || (o.claudeSpan == nil && o.event == "response.failed") {
				o.providerFailed = true
			}
			if !o.overflow {
				o.parse(o.data, true)
			} else {
				// An oversized event still counts, named by its event: field.
				o.events++
				o.lastType = eventName(o.event)
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

// Retain only a top-level usage or count_tokens member. Depth and string
// state span writes, so skipped content can be arbitrarily large.
func (o *tokenObserver) writeClaudeJSON(p []byte, limit int) {
	for _, c := range p {
		if !o.jsonString && o.jsonDepth == 1 && (c == ',' || c == '}') {
			if o.claudeCapture {
				o.parse(append(o.data, '}'), false)
			}
			o.data = nil
			o.claudeCapture = false
			o.claudeKey = c == ','
		} else if o.claudeKey && !o.jsonString && c == ':' {
			var key string
			if json.Unmarshal(o.data, &key) == nil && (key == "usage" || key == "input_tokens") {
				o.claudeCapture = true
				o.data = append(append([]byte{'{'}, o.data...), ':')
			} else {
				o.data = nil
			}
			o.claudeKey = false
		} else if o.claudeCapture || o.claudeKey {
			// Ignore whitespace outside strings to keep padded keys small.
			if !o.jsonString && (c == ' ' || c == '\t' || c == '\r' || c == '\n') {
				continue
			}
			if len(o.data) >= limit || (o.claudeKey && len(o.data) >= 80) {
				o.claudeCapture, o.claudeKey = false, false
				o.data = nil
			} else {
				o.data = append(o.data, c)
			}
		}
		if o.jsonString {
			if o.jsonEscape {
				o.jsonEscape = false
			} else if c == '\\' {
				o.jsonEscape = true
			} else if c == '"' {
				o.jsonString = false
			}
			continue
		}
		switch c {
		case '"':
			o.jsonString = true
		case '{', '[':
			o.jsonDepth++
			if o.jsonDepth == 1 {
				o.claudeKey = true
			}
		case '}', ']':
			o.jsonDepth--
		}
	}
}

func (o *tokenObserver) finish() {
	if !o.sse && o.claudeSpan == nil && !o.overflow {
		o.parse(o.data, false)
	}
}

func (o *tokenObserver) parse(data []byte, sse bool) {
	if o.recorded {
		return
	}
	if o.claudeSpan != nil {
		// Messages streams report input at message_start and cumulative output at message_delta.
		type usage struct {
			Input    *int64 `json:"input_tokens"`
			Cached   *int64 `json:"cache_read_input_tokens"`
			Output   *int64 `json:"output_tokens"`
			Creation int64  `json:"cache_creation_input_tokens"`
		}
		var msg struct {
			Usage   usage
			Message struct{ Usage usage }
			Type    string
			Input   *int64 `json:"input_tokens"`
		}
		if json.Unmarshal(data, &msg) != nil {
			return
		}
		if sse && msg.Type == "error" {
			o.providerFailed = true
		}
		u := msg.Usage
		if msg.Type == "message_start" {
			u = msg.Message.Usage
		}
		if msg.Input != nil {
			u.Input = msg.Input
		}
		if u.Input != nil {
			cached := int64(0)
			if u.Cached != nil {
				cached = *u.Cached
			}
			o.claudeSpan.SetAttributes(attribute.Int64("gen_ai.usage.input_tokens", *u.Input+cached+u.Creation), attribute.Int64("gen_ai.usage.cache_read.input_tokens", cached))
		}
		if u.Output != nil {
			o.claudeSpan.SetAttributes(attribute.Int64("gen_ai.usage.output_tokens", *u.Output))
		}
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
		if event.Type == "response.failed" || event.Type == "error" {
			o.providerFailed = true
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
	span := trace.SpanFromContext(ctx)
	var fallback *tokenObserver
	record := func(u TokenUsage) {
		span.SetAttributes(attribute.Int64("gen_ai.usage.input_tokens", u.Input), attribute.Int64("gen_ai.usage.cache_read.input_tokens", u.CachedInput), attribute.Int64("gen_ai.usage.output_tokens", u.Output))
	}
	if span.IsRecording() {
		fallback = &tokenObserver{onUsage: record, traceOnly: true}
	}
	if rl.TokenUsage == nil {
		return fallback
	}
	// Request cancellation must not discard a completion already read.
	ctx = context.WithoutCancel(ctx)
	start, cancel := context.WithTimeout(ctx, 5*time.Second)
	call, err := rl.TokenUsage.Begin(start, id)
	cancel()
	if err != nil {
		// Error status keeps the trace: the collector keeps every error trace.
		span.AddEvent("codex token accounting start failed")
		span.SetStatus(codes.Error, "token_accounting_start_failed")
		logUntraced(span, rl.Logger, slog.LevelWarn, "codex token accounting start failed")
		rl.Metrics.TokenAccounting.WithLabelValues("start_error").Inc()
		return fallback
	}
	if call == 0 {
		return fallback
	} // not a kagent session actor
	return &tokenObserver{onUsage: func(u TokenUsage) {
		record(u)
		write, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		if err := rl.TokenUsage.Complete(write, call, u); err != nil {
			span.AddEvent("codex token accounting write failed", trace.WithAttributes(attribute.Int64("call", call)))
			span.SetStatus(codes.Error, "token_accounting_write_failed")
			logUntraced(span, rl.Logger, slog.LevelWarn, "codex token accounting write failed", "call", call)
			rl.Metrics.TokenAccounting.WithLabelValues("write_error").Inc()
			return
		}
		rl.Metrics.TokenAccounting.WithLabelValues("recorded").Inc()
	}}
}

// responseEvents are the Responses API stream event types the missing-usage
// log may name. Any other value, which the body controls, logs as "other",
// so the log never carries provider text or credentials.
var responseEvents = map[string]bool{
	"error": true, "response.created": true, "response.in_progress": true, "response.queued": true,
	"response.completed": true, "response.incomplete": true, "response.failed": true,
	"response.output_item.added": true, "response.output_item.done": true,
	"response.content_part.added": true, "response.content_part.done": true,
	"response.output_text.delta": true, "response.output_text.done": true,
	"response.output_text.annotation.added": true,
	"response.refusal.delta":                true, "response.refusal.done": true,
	"response.function_call_arguments.delta": true, "response.function_call_arguments.done": true,
	"response.custom_tool_call_input.delta": true, "response.custom_tool_call_input.done": true,
	"response.reasoning_summary_part.added": true, "response.reasoning_summary_part.done": true,
	"response.reasoning_summary_text.delta": true, "response.reasoning_summary_text.done": true,
	"response.reasoning_text.delta": true, "response.reasoning_text.done": true,
	"response.web_search_call.in_progress": true, "response.web_search_call.searching": true,
	"response.web_search_call.completed": true,
}

func eventName(s string) string {
	switch {
	case s == "":
		return ""
	case responseEvents[s]:
		return s
	default:
		return "other"
	}
}

func isSSE(contentType string) bool {
	typ, _, _ := strings.Cut(contentType, ";")
	return strings.EqualFold(strings.TrimSpace(typ), "text/event-stream")
}
