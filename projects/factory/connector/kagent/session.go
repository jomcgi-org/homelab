package kagent

import (
	"context"
	"errors"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/encoding"
	"google.golang.org/grpc/mem"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
)

// Session runtime states and operations (kagent.api.v1 RuntimeState and
// RuntimeOperation).
const (
	RuntimeReady    = 2
	RuntimeFailed   = 4
	RuntimeDeleting = 5
	RuntimeDeleted  = 6

	OperationRevert = 6
)

// ReasonRuntimeCrashed is the failure reason of a session whose runtime
// crashed; RevertSession restores it.
const ReasonRuntimeCrashed = "RuntimeCrashed"

// ErrSessionNotFound is gRPC NOT_FOUND for a session.
var ErrSessionNotFound = errors.New("kagent: session not found")

// ErrSessionUnrecoverable is gRPC FAILED_PRECONDITION: kagent tells the
// client the session's runtime cannot be recovered (its Actor is missing,
// replaced or retemplated, or the session was deleted) and it must be
// replaced.
var ErrSessionUnrecoverable = errors.New("kagent: session unrecoverable")

// Session is the subset of kagent.api.v1.Session the connector and the
// reviewer read. AgentNamespace and AgentName are the Agent the session was
// created for (its AgentProvenance reference); PreparedRevision is the
// Agent revision kagent pinned the session to.
type Session struct {
	ID               string
	Creator          string
	AgentNamespace   string
	AgentName        string
	PreparedRevision string
	State            int
	Operation        int
	FailureReason    string
	FailureMsg       string
	ContextID        string
	// Name is the caller's display name for the session; CreatedAt is when
	// kagent created it. kagent does not move updated_at when a turn runs, so
	// a session's last activity is read from its tasks.
	Name      string
	CreatedAt time.Time
}

// Crashed reports whether the session needs RevertSession.
func (s *Session) Crashed() bool {
	return s.FailureReason == ReasonRuntimeCrashed ||
		(s.State == RuntimeFailed && s.Operation == OperationRevert)
}

// Gone reports whether the session can no longer take turns.
func (s *Session) Gone() bool {
	return s.State == RuntimeDeleting || s.State == RuntimeDeleted
}

// Sessions calls kagent.api.v1.SessionService. Messages are encoded with
// protowire against the v1.0.1 field numbers, which keeps kagent's module
// graph out of this repository.
type Sessions struct {
	conn   grpc.ClientConnInterface
	closer func() error
}

// DialSessions connects to the controller's gRPC listener in plaintext h2c,
// as the in-cluster Service serves it, sending the projected token per call.
func DialSessions(target string, tokens *TokenSource) (*Sessions, error) {
	conn, err := grpc.NewClient(target,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithPerRPCCredentials(bearer{tokens}),
	)
	if err != nil {
		return nil, err
	}
	return &Sessions{conn: conn, closer: conn.Close}, nil
}

// NewSessions wraps an existing connection, for tests.
func NewSessions(conn grpc.ClientConnInterface) *Sessions { return &Sessions{conn: conn} }

// Close releases the connection.
func (s *Sessions) Close() error {
	if s.closer != nil {
		return s.closer()
	}
	return nil
}

type bearer struct{ tokens *TokenSource }

func (b bearer) GetRequestMetadata(ctx context.Context, _ ...string) (map[string]string, error) {
	if b.tokens == nil {
		return nil, nil
	}
	tok, err := tokenForContext(ctx, b.tokens)
	if err != nil {
		return nil, err
	}
	return map[string]string{"authorization": "Bearer " + tok}, nil
}

func (bearer) RequireTransportSecurity() bool { return false }

// Deadlines for session calls. A revert restores a snapshot and can take
// minutes; kagent bounds one lifecycle attempt at two.
const (
	GetSessionTimeout    = 30 * time.Second
	RevertSessionTimeout = 150 * time.Second
	// DeleteSessionTimeout bounds a delete, which removes the session's
	// runtime and snapshot inline.
	DeleteSessionTimeout = 150 * time.Second
)

// ListSessionsPage is the most sessions one ListSessions call returns.
const ListSessionsPage = 100

// GetSession reads a session.
func (s *Sessions) GetSession(ctx context.Context, id string) (*Session, error) {
	ctx, cancel := context.WithTimeout(ctx, GetSessionTimeout)
	defer cancel()
	return s.invoke(ctx, "/kagent.api.v1.SessionService/GetSession", sessionIDRequest(id))
}

// RevertSession restores a crashed session to its last durable snapshot. It
// returns the session unchanged when its runtime is not crashed.
func (s *Sessions) RevertSession(ctx context.Context, id string) (*Session, error) {
	ctx, cancel := context.WithTimeout(ctx, RevertSessionTimeout)
	defer cancel()
	return s.invoke(ctx, "/kagent.api.v1.SessionService/RevertSession", sessionIDRequest(id))
}

// ListSessions reads one page of the caller's sessions of agent and the
// token of the next page, empty after the last.
func (s *Sessions) ListSessions(ctx context.Context, agent AgentRef, pageToken string) ([]*Session, string, error) {
	ctx, cancel := context.WithTimeout(ctx, GetSessionTimeout)
	defer cancel()
	// ListSessionsRequest { PageRequest page = 3 { int32 limit = 1; string
	// page_token = 2 }; ResourceReference agent = 6 }
	page := protowire.AppendVarint(protowire.AppendTag(nil, 1, protowire.VarintType), ListSessionsPage)
	if pageToken != "" {
		page = protowire.AppendString(protowire.AppendTag(page, 2, protowire.BytesType), pageToken)
	}
	req := protowire.AppendBytes(protowire.AppendTag(nil, 3, protowire.BytesType), page)
	req = protowire.AppendBytes(protowire.AppendTag(req, 6, protowire.BytesType), encodeRef(agent))
	raw := rawMessage(req)
	var resp rawMessage
	if err := s.conn.Invoke(ctx, "/kagent.api.v1.SessionService/ListSessions", &raw, &resp, grpc.ForceCodecV2(rawCodec{})); err != nil {
		return nil, "", err
	}
	// ListSessionsResponse { repeated Session sessions = 1; PageResponse page
	// = 2 { string next_page_token = 1 } }
	var out []*Session
	var next string
	var bad error
	err := walk(resp, func(num protowire.Number, typ protowire.Type, v []byte, _ uint64) {
		switch {
		case typ != protowire.BytesType:
		case num == 1:
			sess, err := decodeSession(v)
			if err != nil {
				bad = err
				return
			}
			out = append(out, sess)
		case num == 2:
			tok, err := field(v, 1)
			if err != nil {
				bad = err
				return
			}
			next = string(tok)
		}
	})
	if err == nil {
		err = bad
	}
	if err != nil {
		return nil, "", err
	}
	return out, next, nil
}

// DeleteSession deletes a session with its runtime and snapshot. kagent
// refuses while the session has an active turn or another lifecycle
// operation; the caller retries later.
func (s *Sessions) DeleteSession(ctx context.Context, id string) (*Session, error) {
	ctx, cancel := context.WithTimeout(ctx, DeleteSessionTimeout)
	defer cancel()
	// DeleteSessionRequest: session_id = 1; force = 2 stays false.
	return s.invoke(ctx, "/kagent.api.v1.SessionService/DeleteSession", sessionIDRequest(id))
}

// NameSession sets the session's display name.
func (s *Sessions) NameSession(ctx context.Context, id, name string) (*Session, error) {
	ctx, cancel := context.WithTimeout(ctx, GetSessionTimeout)
	defer cancel()
	// UpdateSessionNameRequest { session_id = 1; name = 2 }
	req := protowire.AppendString(protowire.AppendTag(sessionIDRequest(id), 2, protowire.BytesType), name)
	return s.invoke(ctx, "/kagent.api.v1.SessionService/UpdateSessionName", req)
}

func sessionIDRequest(id string) []byte {
	return protowire.AppendString(protowire.AppendTag(nil, 1, protowire.BytesType), id)
}

// AgentRevision reads the Agent's status.latestSuccessfulRevision through
// AgentService.GetAgent: the revision kagent pins a new session to, and the
// one its Agent Card describes. It is empty when the Agent has none.
func (s *Sessions) AgentRevision(ctx context.Context, agent AgentRef) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, GetSessionTimeout)
	defer cancel()
	// GetAgentRequest: ResourceReference ref = 1.
	req := rawMessage(protowire.AppendBytes(protowire.AppendTag(nil, 1, protowire.BytesType), encodeRef(agent)))
	var resp rawMessage
	if err := s.conn.Invoke(ctx, "/kagent.api.v1.AgentService/GetAgent", &req, &resp, grpc.ForceCodecV2(rawCodec{})); err != nil {
		return "", err
	}
	// GetAgentResponse { Agent agent = 1 { StructuredObject resource = 2 {
	// google.protobuf.Struct value = 3 } } }, the Agent CR as JSON.
	a, err := field(resp, 1)
	if err != nil {
		return "", err
	}
	res, err := field(a, 2)
	if err != nil {
		return "", err
	}
	cr, err := field(res, 3)
	if err != nil {
		return "", err
	}
	status, err := structField(cr, "status", structValue)
	if err != nil {
		return "", err
	}
	return structString(status, "latestSuccessfulRevision")
}

// google.protobuf.Value kinds the reviewer reads.
const (
	stringValue protowire.Number = 3
	structValue protowire.Number = 5
)

// structField is the given kind of the value under key in an encoded
// google.protobuf.Struct (map<string, Value> fields = 1), or nil when the
// key is missing or holds another kind.
func structField(b []byte, key string, kind protowire.Number) ([]byte, error) {
	var out []byte
	err := walk(b, func(num protowire.Number, typ protowire.Type, entry []byte, _ uint64) {
		if num != 1 || typ != protowire.BytesType {
			return
		}
		k, err := field(entry, 1)
		if err != nil || string(k) != key {
			return
		}
		v, err := field(entry, 2)
		if err != nil {
			return
		}
		out = nil
		_ = walk(v, func(vn protowire.Number, vt protowire.Type, vv []byte, _ uint64) {
			if vn == kind && vt == protowire.BytesType {
				out = vv
			}
		})
	})
	return out, err
}

func structString(b []byte, key string) (string, error) {
	v, err := structField(b, key, stringValue)
	return string(v), err
}

func encodeRef(agent AgentRef) []byte {
	// ResourceReference { namespace = 1; name = 2 }
	var ref []byte
	ref = protowire.AppendTag(ref, 1, protowire.BytesType)
	ref = protowire.AppendString(ref, agent.Namespace)
	ref = protowire.AppendTag(ref, 2, protowire.BytesType)
	return protowire.AppendString(ref, agent.Name)
}

// EncodeAgent encodes a GetAgentResponse whose Agent CR has the given
// status.latestSuccessfulRevision, or no status when it is empty, for test
// servers.
func EncodeAgent(agent AgentRef, revision string) []byte {
	entry := func(key string, kind protowire.Number, value []byte) []byte {
		var v []byte
		v = protowire.AppendTag(v, kind, protowire.BytesType)
		v = protowire.AppendBytes(v, value)
		var e []byte
		e = protowire.AppendTag(e, 1, protowire.BytesType)
		e = protowire.AppendString(e, key)
		e = protowire.AppendTag(e, 2, protowire.BytesType)
		e = protowire.AppendBytes(e, v)
		return protowire.AppendBytes(protowire.AppendTag(nil, 1, protowire.BytesType), e)
	}
	cr := entry("kind", stringValue, []byte("Agent"))
	if revision != "" {
		status := entry("desiredRevision", stringValue, []byte(revision+"-next"))
		status = append(status, entry("latestSuccessfulRevision", stringValue, []byte(revision))...)
		cr = append(cr, entry("status", structValue, status)...)
	}
	var res []byte
	res = protowire.AppendTag(res, 2, protowire.BytesType)
	res = protowire.AppendString(res, "Agent")
	res = protowire.AppendTag(res, 3, protowire.BytesType)
	res = protowire.AppendBytes(res, cr)
	var a []byte
	a = protowire.AppendTag(a, 1, protowire.BytesType)
	a = protowire.AppendBytes(a, encodeRef(agent))
	a = protowire.AppendTag(a, 2, protowire.BytesType)
	a = protowire.AppendBytes(a, res)
	return protowire.AppendBytes(protowire.AppendTag(nil, 1, protowire.BytesType), a)
}

func (s *Sessions) invoke(ctx context.Context, method string, body []byte) (*Session, error) {
	req := rawMessage(body)
	var resp rawMessage
	if err := s.conn.Invoke(ctx, method, &req, &resp, grpc.ForceCodecV2(rawCodec{})); err != nil {
		switch status.Code(err) {
		case codes.NotFound:
			return nil, fmt.Errorf("%w: %v", ErrSessionNotFound, err)
		case codes.FailedPrecondition:
			return nil, fmt.Errorf("%w: %v", ErrSessionUnrecoverable, err)
		}
		return nil, err
	}
	// Each session call's response holds Session session = 1.
	body, err := field(resp, 1)
	if err != nil {
		return nil, err
	}
	return decodeSession(body)
}

// EncodeSession encodes a Session response body, for test servers.
func EncodeSession(sess Session) []byte {
	var b []byte
	b = protowire.AppendTag(b, 1, protowire.BytesType)
	b = protowire.AppendString(b, sess.ID)
	if sess.Creator != "" {
		b = protowire.AppendTag(b, 2, protowire.BytesType)
		b = protowire.AppendString(b, sess.Creator)
	}
	if sess.PreparedRevision != "" {
		b = protowire.AppendTag(b, 5, protowire.BytesType)
		b = protowire.AppendString(b, sess.PreparedRevision)
	}
	b = protowire.AppendTag(b, 7, protowire.VarintType)
	b = protowire.AppendVarint(b, uint64(sess.State))
	b = protowire.AppendTag(b, 8, protowire.VarintType)
	b = protowire.AppendVarint(b, uint64(sess.Operation))
	if sess.FailureReason != "" || sess.FailureMsg != "" {
		var f []byte
		f = protowire.AppendTag(f, 1, protowire.BytesType)
		f = protowire.AppendString(f, sess.FailureReason)
		f = protowire.AppendTag(f, 2, protowire.BytesType)
		f = protowire.AppendString(f, sess.FailureMsg)
		b = protowire.AppendTag(b, 9, protowire.BytesType)
		b = protowire.AppendBytes(b, f)
	}
	if !sess.CreatedAt.IsZero() {
		// google.protobuf.Timestamp created_at = 10 { seconds = 1; nanos = 2 }
		var ts []byte
		ts = protowire.AppendTag(ts, 1, protowire.VarintType)
		ts = protowire.AppendVarint(ts, uint64(sess.CreatedAt.Unix()))
		ts = protowire.AppendTag(ts, 2, protowire.VarintType)
		ts = protowire.AppendVarint(ts, uint64(sess.CreatedAt.Nanosecond()))
		b = protowire.AppendTag(b, 10, protowire.BytesType)
		b = protowire.AppendBytes(b, ts)
	}
	if sess.Name != "" {
		b = protowire.AppendTag(b, 13, protowire.BytesType)
		b = protowire.AppendString(b, sess.Name)
	}
	b = protowire.AppendTag(b, 14, protowire.BytesType)
	b = protowire.AppendString(b, sess.ContextID)
	if sess.AgentNamespace != "" || sess.AgentName != "" {
		// AgentProvenance agent = 15 { ResourceReference ref = 1 }
		var agent []byte
		agent = protowire.AppendTag(agent, 1, protowire.BytesType)
		agent = protowire.AppendBytes(agent, encodeRef(AgentRef{Namespace: sess.AgentNamespace, Name: sess.AgentName}))
		b = protowire.AppendTag(b, 15, protowire.BytesType)
		b = protowire.AppendBytes(b, agent)
	}
	out := protowire.AppendTag(nil, 1, protowire.BytesType)
	return protowire.AppendBytes(out, b)
}

// EncodeSessionList encodes a ListSessions response body, for test servers.
func EncodeSessionList(sessions []Session, next string) []byte {
	var out []byte
	for _, sess := range sessions {
		out = append(out, EncodeSession(sess)...) // Each is field 1, the repeated sessions.
	}
	if next != "" {
		page := protowire.AppendString(protowire.AppendTag(nil, 1, protowire.BytesType), next)
		out = protowire.AppendBytes(protowire.AppendTag(out, 2, protowire.BytesType), page)
	}
	return out
}

// DecodeListSessions reads the page token and agent of a ListSessions
// request, for test servers.
func DecodeListSessions(b []byte) (pageToken string, agent AgentRef, err error) {
	page, err := field(b, 3)
	if err != nil {
		return "", AgentRef{}, err
	}
	tok, err := field(page, 2)
	if err != nil {
		return "", AgentRef{}, err
	}
	ref, err := field(b, 6)
	if err != nil {
		return "", AgentRef{}, err
	}
	ns, _ := field(ref, 1)
	name, _ := field(ref, 2)
	return string(tok), AgentRef{Namespace: string(ns), Name: string(name)}, nil
}

func decodeSession(b []byte) (*Session, error) {
	s := &Session{}
	err := walk(b, func(num protowire.Number, typ protowire.Type, v []byte, n uint64) {
		switch {
		case num == 1 && typ == protowire.BytesType:
			s.ID = string(v)
		case num == 2 && typ == protowire.BytesType:
			s.Creator = string(v)
		case num == 5 && typ == protowire.BytesType:
			s.PreparedRevision = string(v)
		case num == 7 && typ == protowire.VarintType:
			s.State = int(n)
		case num == 8 && typ == protowire.VarintType:
			s.Operation = int(n)
		case num == 14 && typ == protowire.BytesType:
			s.ContextID = string(v)
		case num == 13 && typ == protowire.BytesType:
			s.Name = string(v)
		case num == 10 && typ == protowire.BytesType:
			var sec, nsec uint64
			_ = walk(v, func(fn protowire.Number, ft protowire.Type, _ []byte, fv uint64) {
				switch {
				case fn == 1 && ft == protowire.VarintType:
					sec = fv
				case fn == 2 && ft == protowire.VarintType:
					nsec = fv
				}
			})
			s.CreatedAt = time.Unix(int64(sec), int64(nsec)).UTC()
		case num == 15 && typ == protowire.BytesType:
			ref, err := field(v, 1)
			if err != nil {
				return
			}
			_ = walk(ref, func(fn protowire.Number, ft protowire.Type, fv []byte, _ uint64) {
				if ft != protowire.BytesType {
					return
				}
				switch fn {
				case 1:
					s.AgentNamespace = string(fv)
				case 2:
					s.AgentName = string(fv)
				}
			})
		case num == 9 && typ == protowire.BytesType:
			_ = walk(v, func(fn protowire.Number, ft protowire.Type, fv []byte, _ uint64) {
				if ft != protowire.BytesType {
					return
				}
				switch fn {
				case 1:
					s.FailureReason = string(fv)
				case 2:
					s.FailureMsg = string(fv)
				}
			})
		}
	})
	return s, err
}

// DecodeSessionID reads session_id (field 1) from a request body, for test
// servers.
func DecodeSessionID(b []byte) (string, error) {
	v, err := field(b, 1)
	return string(v), err
}

func field(b []byte, want protowire.Number) ([]byte, error) {
	var out []byte
	err := walk(b, func(num protowire.Number, typ protowire.Type, v []byte, _ uint64) {
		if num == want && typ == protowire.BytesType {
			out = v
		}
	})
	return out, err
}

func walk(b []byte, fn func(protowire.Number, protowire.Type, []byte, uint64)) error {
	for len(b) > 0 {
		num, typ, n := protowire.ConsumeTag(b)
		if n < 0 {
			return protowire.ParseError(n)
		}
		b = b[n:]
		switch typ {
		case protowire.VarintType:
			v, m := protowire.ConsumeVarint(b)
			if m < 0 {
				return protowire.ParseError(m)
			}
			fn(num, typ, nil, v)
			b = b[m:]
		case protowire.BytesType:
			v, m := protowire.ConsumeBytes(b)
			if m < 0 {
				return protowire.ParseError(m)
			}
			fn(num, typ, v, 0)
			b = b[m:]
		default:
			m := protowire.ConsumeFieldValue(num, typ, b)
			if m < 0 {
				return protowire.ParseError(m)
			}
			b = b[m:]
		}
	}
	return nil
}

// rawMessage is a pre-encoded protobuf message.
type rawMessage []byte

// rawCodec passes rawMessage bytes through unchanged. It is named "proto"
// so the content type is application/grpc+proto.
type rawCodec struct{}

func (rawCodec) Marshal(v any) (mem.BufferSlice, error) {
	m, ok := v.(*rawMessage)
	if !ok {
		return nil, fmt.Errorf("rawCodec: unexpected %T", v)
	}
	return mem.BufferSlice{mem.SliceBuffer(append([]byte(nil), *m...))}, nil
}

func (rawCodec) Unmarshal(data mem.BufferSlice, v any) error {
	m, ok := v.(*rawMessage)
	if !ok {
		return fmt.Errorf("rawCodec: unexpected %T", v)
	}
	*m = data.Materialize()
	return nil
}

func (rawCodec) Name() string { return "proto" }

// RawCodec is the pass-through codec, for test servers.
func RawCodec() encoding.CodecV2 { return rawCodec{} }

// RawMessage wraps bytes for a test server's handler.
type RawMessage = rawMessage
