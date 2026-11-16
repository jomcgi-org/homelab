package kagent

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type rpcSeen struct {
	path, auth, version string
	method              string
	params              map[string]any
}

func a2aServer(t *testing.T, reply string, got *rpcSeen) *A2A {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.path, got.auth, got.version = r.URL.Path, r.Header.Get("Authorization"), r.Header.Get("A2A-Version")
		b, _ := io.ReadAll(r.Body)
		var req struct {
			Method string         `json:"method"`
			Params map[string]any `json:"params"`
		}
		if err := json.Unmarshal(b, &req); err != nil {
			t.Errorf("request: %v", err)
		}
		got.method, got.params = req.Method, req.Params
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, reply)
	}))
	t.Cleanup(srv.Close)
	tok := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tok, []byte("sa-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return &A2A{BaseURL: srv.URL, Tokens: &TokenSource{Path: tok}}
}

var ref = AgentRef{Namespace: "factory-homelab", Name: "coder"}

func TestSendUsesA2AV1AndDeterministicMessageID(t *testing.T) {
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","result":{"task":{"id":"t1","contextId":"c1","status":{"state":"TASK_STATE_SUBMITTED"}}}}`, &got)
	task, err := c.Send(context.Background(), ref, Message{MessageID: "itsaplan-run/7/initial", Parts: []Part{{Text: "hi"}}})
	if err != nil {
		t.Fatal(err)
	}
	if got.path != "/agents/factory-homelab/coder" || got.method != "SendMessage" || got.version != "1.0" || got.auth != "Bearer sa-token" {
		t.Fatalf("request = %+v", got)
	}
	msg, _ := got.params["message"].(map[string]any)
	if msg["messageId"] != "itsaplan-run/7/initial" || msg["role"] != "ROLE_USER" {
		t.Fatalf("message = %+v", msg)
	}
	if cfg, _ := got.params["configuration"].(map[string]any); cfg["returnImmediately"] != true {
		t.Fatalf("configuration = %+v", got.params["configuration"])
	}
	if task.ID != "t1" || task.ContextID != "c1" {
		t.Fatalf("task = %+v", task)
	}
}

func TestSendNotAcceptedIsRetryable(t *testing.T) {
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","error":{"code":-32603,"message":"busy","data":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo","reason":"INTERNAL_ERROR","domain":"a2a-protocol.org","metadata":{"reason":"KAGENT_SEND_NOT_ACCEPTED","retryAfterMs":"100"}}]}}`, &got)
	if _, err := c.Send(context.Background(), ref, Message{MessageID: "m"}); !errors.Is(err, ErrNotAccepted) {
		t.Fatalf("err = %v", err)
	}
}

func TestSendBusySessionIsNotAccepted(t *testing.T) {
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","error":{"code":-32004,"message":"Session cannot accept work during a lifecycle operation"}}`, &got)
	if _, err := c.Send(context.Background(), ref, Message{MessageID: "m", ContextID: "c1"}); !errors.Is(err, ErrNotAccepted) {
		t.Fatalf("err = %v", err)
	}
}

func TestSendInvalidRequestIsRejected(t *testing.T) {
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","error":{"code":-32600,"message":"message ID was already used with a different request"}}`, &got)
	if _, err := c.Send(context.Background(), ref, Message{MessageID: "m"}); !errors.Is(err, ErrRejected) {
		t.Fatalf("err = %v", err)
	}
}

func TestSendTransportErrorIsAmbiguous(t *testing.T) {
	c := &A2A{BaseURL: "http://127.0.0.1:1"}
	if _, err := c.Send(context.Background(), ref, Message{MessageID: "m"}); !errors.Is(err, ErrAmbiguous) {
		t.Fatalf("err = %v", err)
	}
}

func TestSendContinuationNotFound(t *testing.T) {
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","error":{"code":-32001,"message":"task not found"}}`, &got)
	if _, err := c.Send(context.Background(), ref, Message{MessageID: "m", ContextID: "c1"}); !errors.Is(err, ErrTaskNotFound) {
		t.Fatalf("err = %v", err)
	}
}

func TestListGetCancel(t *testing.T) {
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","result":{"tasks":[{"id":"t1","contextId":"c1","status":{"state":"TASK_STATE_WORKING"},"history":[{"messageId":"itsaplan-run/7/initial","role":"ROLE_USER","parts":[{"text":"hi"}]}]}],"totalSize":1,"pageSize":100,"nextPageToken":""}}`, &got)
	page, err := c.ListTasks(context.Background(), ref, "c1", StateWorking, "")
	if err != nil || page.NextPageToken != "" || len(page.Tasks) != 1 || page.TotalSize != 1 || page.PageSize != 100 {
		t.Fatalf("page = %+v err = %v", page, err)
	}
	if got.method != "ListTasks" || got.params["contextId"] != "c1" || got.params["status"] != StateWorking || got.params["historyLength"] != float64(0) {
		t.Fatalf("request = %+v", got)
	}

	c = a2aServer(t, `{"jsonrpc":"2.0","id":"1","result":{"id":"t1","contextId":"c1","status":{"state":"TASK_STATE_CANCELED"}}}`, &got)
	task, err := c.CancelTask(context.Background(), ref, "t1")
	if err != nil || got.method != "CancelTask" || got.params["id"] != "t1" || task.Status.State != StateCanceled {
		t.Fatalf("cancel = %+v %+v %v", got, task, err)
	}
	if _, err := c.GetTask(context.Background(), ref, "t1"); err != nil || got.method != "GetTask" {
		t.Fatalf("get = %+v %v", got, err)
	}
}

func TestListTasksRejectsIncompleteResponses(t *testing.T) {
	for name, result := range map[string]string{
		"null result":       `null`,
		"empty object":      `{}`,
		"wrong result type": `[]`,
		"missing tasks":     `{"totalSize":0,"pageSize":100,"nextPageToken":""}`,
		"null tasks":        `{"tasks":null,"totalSize":0,"pageSize":100,"nextPageToken":""}`,
		"wrong tasks type":  `{"tasks":{},"totalSize":0,"pageSize":100,"nextPageToken":""}`,
		"missing cursor":    `{"tasks":[],"totalSize":0,"pageSize":100}`,
		"null cursor":       `{"tasks":[],"totalSize":0,"pageSize":100,"nextPageToken":null}`,
		"wrong cursor type": `{"tasks":[],"totalSize":0,"pageSize":100,"nextPageToken":4}`,
		"missing total":     `{"tasks":[],"pageSize":100,"nextPageToken":""}`,
		"null total":        `{"tasks":[],"totalSize":null,"pageSize":100,"nextPageToken":""}`,
		"negative total":    `{"tasks":[],"totalSize":-1,"pageSize":100,"nextPageToken":""}`,
		"missing page size": `{"tasks":[],"totalSize":0,"nextPageToken":""}`,
		"null page size":    `{"tasks":[],"totalSize":0,"pageSize":null,"nextPageToken":""}`,
		"zero page size":    `{"tasks":[],"totalSize":0,"pageSize":0,"nextPageToken":""}`,
		"oversized page":    `{"tasks":[],"totalSize":0,"pageSize":101,"nextPageToken":""}`,
		"too many rows":     `{"tasks":[{"id":"a"},{"id":"b"}],"totalSize":2,"pageSize":1,"nextPageToken":""}`,
		"short cursor page": `{"tasks":[{"id":"a"}],"totalSize":2,"pageSize":100,"nextPageToken":"next"}`,
		"empty cursor page": `{"tasks":[],"totalSize":1,"pageSize":100,"nextPageToken":"next"}`,
	} {
		t.Run(name, func(t *testing.T) {
			var got rpcSeen
			c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","result":`+result+`}`, &got)
			page, err := c.ListTasks(context.Background(), ref, "", "", "")
			if err == nil || page != nil {
				t.Fatalf("malformed page accepted: page=%+v err=%v", page, err)
			}
		})
	}
	t.Run("missing result", func(t *testing.T) {
		var got rpcSeen
		c := a2aServer(t, `{"jsonrpc":"2.0","id":"1"}`, &got)
		if _, err := c.ListTasks(context.Background(), ref, "", "", ""); err == nil {
			t.Fatal("missing result accepted as an empty page")
		}
	})
}

func TestListTasksAcceptsEmptyKagentPage(t *testing.T) {
	// kagent v1.0.1 returns a nonnil empty slice. Its pinned a2a-go v2.7.0
	// ListTasksResponse encodes all four fields without omitempty.
	var got rpcSeen
	c := a2aServer(t, `{"jsonrpc":"2.0","id":"1","result":{"tasks":[],"totalSize":0,"pageSize":100,"nextPageToken":""}}`, &got)
	page, err := c.ListTasks(context.Background(), ref, "", "", "")
	if err != nil || page.Tasks == nil || len(page.Tasks) != 0 || page.NextPageToken != "" || page.TotalSize != 0 {
		t.Fatalf("valid empty page rejected: page=%+v err=%v", page, err)
	}
}

func TestSessionsOverGRPC(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var methods []string
	var auth []string
	srv := grpc.NewServer(grpc.ForceServerCodecV2(RawCodec()), grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		method, _ := grpc.MethodFromServerStream(stream)
		methods = append(methods, method)
		if md, ok := metadata.FromIncomingContext(stream.Context()); ok {
			auth = append(auth, md.Get("authorization")...)
		}
		var req RawMessage
		if err := stream.RecvMsg(&req); err != nil {
			return err
		}
		if method == "/kagent.api.v1.AgentService/GetAgent" {
			ref, err := field(req, 1)
			if err != nil {
				return err
			}
			ns, _ := field(ref, 1)
			name, _ := field(ref, 2)
			revision := "agent-rev-7"
			if string(name) != "homelab-reviewer" {
				revision = "" // an Agent with no successful revision
			}
			resp := RawMessage(EncodeAgent(AgentRef{Namespace: string(ns), Name: string(name)}, revision))
			return stream.SendMsg(&resp)
		}
		id, err := DecodeSessionID(req)
		if err != nil {
			return err
		}
		sess := Session{
			ID: id, ContextID: id, State: RuntimeFailed, Operation: OperationRevert, FailureReason: ReasonRuntimeCrashed,
			AgentNamespace: "factory-r-homelab-review", AgentName: "homelab-reviewer", PreparedRevision: "agent-rev-6",
		}
		if method == "/kagent.api.v1.SessionService/RevertSession" {
			sess = Session{ID: id, ContextID: id, State: RuntimeReady, Operation: 5}
		}
		resp := RawMessage(EncodeSession(sess))
		return stream.SendMsg(&resp)
	}))
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	tok := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tok, []byte("sa-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := DialSessions(lis.Addr().String(), &TokenSource{Path: tok})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	id := "0b8f1e64-8d43-4f43-9b0b-2f1e5a4c7d10"
	s, err := c.GetSession(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	if s.ID != id || !s.Crashed() || s.FailureReason != ReasonRuntimeCrashed ||
		s.AgentNamespace != "factory-r-homelab-review" || s.AgentName != "homelab-reviewer" || s.PreparedRevision != "agent-rev-6" {
		t.Fatalf("session = %+v", s)
	}
	s, err = c.RevertSession(context.Background(), id)
	if err != nil || s.Crashed() || s.State != RuntimeReady || s.AgentNamespace != "" || s.PreparedRevision != "" {
		t.Fatalf("revert = %+v %v", s, err)
	}
	rev, err := c.AgentRevision(context.Background(), AgentRef{Namespace: "factory-r-homelab-review", Name: "homelab-reviewer"})
	if err != nil || rev != "agent-rev-7" {
		t.Fatalf("agent revision = %q, %v", rev, err)
	}
	if rev, err := c.AgentRevision(context.Background(), AgentRef{Namespace: "factory-r-homelab-review", Name: "unready"}); err != nil || rev != "" {
		t.Fatalf("agent without a revision = %q, %v", rev, err)
	}
	want := []string{
		"/kagent.api.v1.SessionService/GetSession", "/kagent.api.v1.SessionService/RevertSession",
		"/kagent.api.v1.AgentService/GetAgent", "/kagent.api.v1.AgentService/GetAgent",
	}
	if len(methods) != len(want) {
		t.Fatalf("methods = %v", methods)
	}
	for i := range want {
		if methods[i] != want[i] {
			t.Fatalf("methods = %v", methods)
		}
	}
	if len(auth) != 4 || auth[0] != "Bearer sa-token" || auth[2] != "Bearer sa-token" {
		t.Fatalf("authorization = %v", auth)
	}
}

func TestUnrecoverableSessionIsFailedPrecondition(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer(grpc.ForceServerCodecV2(RawCodec()), grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		var req RawMessage
		if err := stream.RecvMsg(&req); err != nil {
			return err
		}
		return status.Error(codes.FailedPrecondition, "runtime cannot be recovered: runtime Actor is missing")
	}))
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	c, err := DialSessions(lis.Addr().String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if _, err := c.RevertSession(context.Background(), "s1"); !errors.Is(err, ErrSessionUnrecoverable) {
		t.Fatalf("revert = %v, want ErrSessionUnrecoverable", err)
	}
}

func TestSessionListDeleteAndNameOverGRPC(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	created := time.Date(2026, 11, 16, 1, 52, 28, 500, time.UTC)
	agent := AgentRef{Namespace: "factory-r-homelab-review", Name: "homelab-reviewer"}
	var calls []string
	srv := grpc.NewServer(grpc.ForceServerCodecV2(RawCodec()), grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		method, _ := grpc.MethodFromServerStream(stream)
		var req RawMessage
		if err := stream.RecvMsg(&req); err != nil {
			return err
		}
		var resp RawMessage
		switch method {
		case "/kagent.api.v1.SessionService/ListSessions":
			tok, ref, err := DecodeListSessions(req)
			if err != nil {
				return err
			}
			calls = append(calls, "list:"+tok+":"+ref.Namespace+"/"+ref.Name)
			if tok == "" {
				resp = EncodeSessionList([]Session{
					{ID: "s1", ContextID: "s1", Name: "factory-review/o/r/7", CreatedAt: created, State: RuntimeReady, AgentNamespace: agent.Namespace, AgentName: agent.Name},
					{ID: "s2", ContextID: "s2", State: RuntimeReady},
				}, "p2")
			} else {
				resp = EncodeSessionList([]Session{{ID: "s3", ContextID: "s3", State: RuntimeDeleting}}, "")
			}
		case "/kagent.api.v1.SessionService/DeleteSession":
			id, _ := DecodeSessionID(req)
			if len(req) != 2+len(id) { // session_id alone: force stays unset.
				return status.Error(codes.InvalidArgument, "force must stay unset")
			}
			calls = append(calls, "delete:"+id)
			if id == "busy" {
				return status.Error(codes.FailedPrecondition, "session has active turns")
			}
			resp = EncodeSession(Session{ID: id, ContextID: id, State: RuntimeDeleting})
		case "/kagent.api.v1.SessionService/UpdateSessionName":
			id, _ := DecodeSessionID(req)
			name, _ := field(req, 2)
			calls = append(calls, "name:"+id+":"+string(name))
			resp = EncodeSession(Session{ID: id, ContextID: id, Name: string(name), State: RuntimeReady})
		default:
			return status.Error(codes.Unimplemented, method)
		}
		return stream.SendMsg(&resp)
	}))
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	c, err := DialSessions(lis.Addr().String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	ctx := context.Background()

	page, next, err := c.ListSessions(ctx, agent, "")
	if err != nil || next != "p2" || len(page) != 2 {
		t.Fatalf("first page = %+v %q %v", page, next, err)
	}
	if s := page[0]; s.ID != "s1" || s.Name != "factory-review/o/r/7" || !s.CreatedAt.Equal(created) || s.AgentName != agent.Name {
		t.Fatalf("first session = %+v", s)
	}
	if s := page[1]; s.ID != "s2" || s.Name != "" || !s.CreatedAt.IsZero() {
		t.Fatalf("second session = %+v", s)
	}
	page, next, err = c.ListSessions(ctx, agent, "p2")
	if err != nil || next != "" || len(page) != 1 || !page[0].Gone() {
		t.Fatalf("last page = %+v %q %v", page, next, err)
	}
	if s, err := c.DeleteSession(ctx, "s1"); err != nil || !s.Gone() {
		t.Fatalf("delete = %+v %v", s, err)
	}
	if _, err := c.DeleteSession(ctx, "busy"); err == nil {
		t.Fatal("a refused delete returned no error")
	}
	if s, err := c.NameSession(ctx, "s2", "factory-review/o/r/8"); err != nil || s.Name != "factory-review/o/r/8" {
		t.Fatalf("name = %+v %v", s, err)
	}
	want := []string{
		"list::factory-r-homelab-review/homelab-reviewer", "list:p2:factory-r-homelab-review/homelab-reviewer",
		"delete:s1", "delete:busy", "name:s2:factory-review/o/r/8",
	}
	if !slices.Equal(calls, want) {
		t.Fatalf("calls = %v, want %v", calls, want)
	}
}
