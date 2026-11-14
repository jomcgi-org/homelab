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

// Session is the subset of kagent.api.v1.Session the connector reads.
type Session struct {
	ID            string
	State         int
	Operation     int
	FailureReason string
	FailureMsg    string
	ContextID     string
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

func (b bearer) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	if b.tokens == nil {
		return nil, nil
	}
	tok, err := b.tokens.Token()
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
)

// GetSession reads a session.
func (s *Sessions) GetSession(ctx context.Context, id string) (*Session, error) {
	ctx, cancel := context.WithTimeout(ctx, GetSessionTimeout)
	defer cancel()
	return s.invoke(ctx, "/kagent.api.v1.SessionService/GetSession", id)
}

// RevertSession restores a crashed session to its last durable snapshot. It
// returns the session unchanged when its runtime is not crashed.
func (s *Sessions) RevertSession(ctx context.Context, id string) (*Session, error) {
	ctx, cancel := context.WithTimeout(ctx, RevertSessionTimeout)
	defer cancel()
	return s.invoke(ctx, "/kagent.api.v1.SessionService/RevertSession", id)
}

func (s *Sessions) invoke(ctx context.Context, method, id string) (*Session, error) {
	// GetSessionRequest and RevertSessionRequest: string session_id = 1.
	req := rawMessage(protowire.AppendString(protowire.AppendTag(nil, 1, protowire.BytesType), id))
	var resp rawMessage
	if err := s.conn.Invoke(ctx, method, &req, &resp, grpc.ForceCodecV2(rawCodec{})); err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, fmt.Errorf("%w: %v", ErrSessionNotFound, err)
		}
		return nil, err
	}
	// GetSessionResponse and RevertSessionResponse: Session session = 1.
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
	b = protowire.AppendTag(b, 14, protowire.BytesType)
	b = protowire.AppendString(b, sess.ContextID)
	out := protowire.AppendTag(nil, 1, protowire.BytesType)
	return protowire.AppendBytes(out, b)
}

func decodeSession(b []byte) (*Session, error) {
	s := &Session{}
	err := walk(b, func(num protowire.Number, typ protowire.Type, v []byte, n uint64) {
		switch {
		case num == 1 && typ == protowire.BytesType:
			s.ID = string(v)
		case num == 7 && typ == protowire.VarintType:
			s.State = int(n)
		case num == 8 && typ == protowire.VarintType:
			s.Operation = int(n)
		case num == 14 && typ == protowire.BytesType:
			s.ContextID = string(v)
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
