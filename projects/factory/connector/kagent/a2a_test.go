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
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
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
	tasks, next, err := c.ListTasks(context.Background(), ref, "c1", StateWorking, "")
	if err != nil || next != "" || len(tasks) != 1 {
		t.Fatalf("tasks = %+v next = %q err = %v", tasks, next, err)
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
		id, err := DecodeSessionID(req)
		if err != nil {
			return err
		}
		sess := Session{ID: id, ContextID: id, State: RuntimeFailed, Operation: OperationRevert, FailureReason: ReasonRuntimeCrashed}
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
	if s.ID != id || !s.Crashed() || s.FailureReason != ReasonRuntimeCrashed {
		t.Fatalf("session = %+v", s)
	}
	s, err = c.RevertSession(context.Background(), id)
	if err != nil || s.Crashed() || s.State != RuntimeReady {
		t.Fatalf("revert = %+v %v", s, err)
	}
	want := []string{"/kagent.api.v1.SessionService/GetSession", "/kagent.api.v1.SessionService/RevertSession"}
	if len(methods) != 2 || methods[0] != want[0] || methods[1] != want[1] {
		t.Fatalf("methods = %v", methods)
	}
	if len(auth) != 2 || auth[0] != "Bearer sa-token" {
		t.Fatalf("authorization = %v", auth)
	}
}
