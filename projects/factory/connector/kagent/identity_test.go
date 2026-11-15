package kagent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

func projectedTestToken(t *testing.T, issuer, uid, rotation string) string {
	t.Helper()
	claims := map[string]any{
		"iss": issuer, "sub": "system:serviceaccount:factory:factory-connector",
		"aud": []string{"kagent-invocation"}, "jti": rotation,
		"kubernetes.io": map[string]any{
			"namespace":      "factory",
			"serviceaccount": map[string]any{"name": "factory-connector", "uid": uid},
		},
	}
	b, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	return "eyJhbGciOiJSUzI1NiJ9." + base64.RawURLEncoding.EncodeToString(b) + ".test-signature"
}

func writeProjectedToken(t *testing.T, path, token string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(token+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestCallerIdentitySurvivesTokenRotationButFencesNewCaller(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	writeProjectedToken(t, path, projectedTestToken(t, "https://cluster.example", "sa-1", "first"))
	c := InvocationCredentials{
		Tokens: &TokenSource{Path: path}, Authority: "factory",
		A2AEndpoint: "http://kagent.kagent.svc:8083", GRPCTarget: "kagent.kagent.svc:8083",
	}
	_, first, err := c.Pin(context.Background())
	if err != nil || !first.Valid() {
		t.Fatalf("identity = %+v, %v", first, err)
	}
	wantCreator := base64.RawURLEncoding.EncodeToString([]byte("kubernetes:factory")) + "." + base64.RawURLEncoding.EncodeToString([]byte("sa-1"))
	if first.PrincipalID() != wantCreator {
		t.Fatalf("creator = %q, want %q", first.PrincipalID(), wantCreator)
	}
	var roundTrip CallerIdentity
	if err := json.Unmarshal([]byte(first.Key()), &roundTrip); err != nil || roundTrip != first {
		t.Fatalf("identity key round trip = %+v, %v", roundTrip, err)
	}
	writeProjectedToken(t, path, projectedTestToken(t, "https://cluster.example", "sa-1", "rotated"))
	_, rotated, err := c.Pin(context.Background())
	if err != nil || rotated != first || rotated.Key() != first.Key() {
		t.Fatalf("rotation changed identity: %+v, %v", rotated, err)
	}
	for _, field := range []string{"uid", "issuer", "authority", "a2aEndpoint", "grpcTarget"} {
		t.Run(field, func(t *testing.T) {
			changed := c
			issuer, uid := "https://cluster.example", "sa-1"
			switch field {
			case "uid":
				uid = "sa-2"
			case "issuer":
				issuer = "https://restored.example"
			case "authority":
				changed.Authority = "replacement"
			case "a2aEndpoint":
				changed.A2AEndpoint = "http://replacement.example"
			case "grpcTarget":
				changed.GRPCTarget = "replacement.example:8083"
			}
			writeProjectedToken(t, path, projectedTestToken(t, issuer, uid, "changed"))
			_, got, err := changed.Pin(context.Background())
			if err != nil || got.Key() == first.Key() {
				t.Fatalf("changed boundary not detected: %+v, %v", got, err)
			}
		})
	}
}

func TestPinnedTokenIsSharedByHTTPAndGRPCDespiteFileRotation(t *testing.T) {
	httpAuth := make(chan string, 2)
	httpServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		httpAuth <- r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"jsonrpc":"2.0","id":"1","result":{"task":{"id":"t1","contextId":"c1","status":{"state":"TASK_STATE_WORKING"}}}}`)
	}))
	t.Cleanup(httpServer.Close)
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcAuth := make(chan string, 1)
	const creator = "authenticated-session-owner"
	server := grpc.NewServer(grpc.ForceServerCodecV2(RawCodec()), grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		md, _ := metadata.FromIncomingContext(stream.Context())
		grpcAuth <- md.Get("authorization")[0]
		var req RawMessage
		if err := stream.RecvMsg(&req); err != nil {
			return err
		}
		resp := RawMessage(EncodeSession(Session{ID: "c1", ContextID: "c1", Creator: creator, State: RuntimeReady}))
		return stream.SendMsg(&resp)
	}))
	go func() { _ = server.Serve(lis) }()
	t.Cleanup(server.Stop)
	path := filepath.Join(t.TempDir(), "token")
	first := projectedTestToken(t, "https://cluster.example", "sa-1", "first")
	second := projectedTestToken(t, "https://cluster.example", "sa-2", "second")
	writeProjectedToken(t, path, first)
	source := &TokenSource{Path: path, refresh: -time.Second}
	credentials := InvocationCredentials{Tokens: source, Authority: "factory", A2AEndpoint: httpServer.URL, GRPCTarget: lis.Addr().String()}
	ctx, originalIdentity, err := credentials.Pin(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	writeProjectedToken(t, path, second)
	nested, nestedIdentity, err := credentials.Pin(ctx)
	if err != nil || nested != ctx || nestedIdentity != originalIdentity {
		t.Fatalf("nested pin changed identity: %+v, %v", nestedIdentity, err)
	}
	changedConfig := credentials
	changedConfig.Authority = "other"
	if _, _, err := changedConfig.Pin(ctx); !errors.Is(err, ErrCallerIdentity) {
		t.Fatal("nested pin accepted a changed identity boundary")
	}
	a2a := &A2A{BaseURL: httpServer.URL, Tokens: source}
	if _, err := a2a.Send(ctx, ref, Message{MessageID: "original"}); err != nil {
		t.Fatal(err)
	}
	sessions, err := DialSessions(lis.Addr().String(), source)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sessions.Close() })
	session, err := sessions.GetSession(ctx, "c1")
	if err != nil || session.Creator != creator {
		t.Fatalf("session creator = %+v, %v", session, err)
	}
	if got := <-httpAuth; got != "Bearer "+first {
		t.Fatal("HTTP request did not retain the pinned token")
	}
	if got := <-grpcAuth; got != "Bearer "+first {
		t.Fatal("gRPC request did not retain the pinned token")
	}
	if _, err := a2a.Send(context.Background(), ref, Message{MessageID: "unpinned"}); err != nil {
		t.Fatal(err)
	}
	if got := <-httpAuth; got != "Bearer "+second {
		t.Fatal("unpinned request did not read the replacement token")
	}
	otherPath := filepath.Join(t.TempDir(), "broker-token")
	writeProjectedToken(t, otherPath, "other-source-token")
	if got, err := tokenForContext(ctx, &TokenSource{Path: otherPath}); err != nil || got != "other-source-token" {
		t.Fatal("a different token source borrowed the pinned token")
	}
}

func TestCallerIdentityRejectsMalformedCredentialWithoutEchoingIt(t *testing.T) {
	valid := projectedTestToken(t, "https://cluster.example", "sa-1", "do-not-echo")
	parts := strings.Split(valid, ".")
	body, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []string{"not-jwt", "bad-base64", "no-issuer", "no-uid", "wrong-audience", "wrong-subject", "wrong-namespace", "null-audience"} {
		t.Run(test, func(t *testing.T) {
			var claims map[string]any
			if err := json.Unmarshal(body, &claims); err != nil {
				t.Fatal(err)
			}
			kubernetes := claims["kubernetes.io"].(map[string]any)
			switch test {
			case "no-issuer":
				delete(claims, "iss")
			case "no-uid":
				delete(kubernetes["serviceaccount"].(map[string]any), "uid")
			case "wrong-audience":
				claims["aud"] = []string{"factory-broker"}
			case "wrong-subject":
				claims["sub"] = "some-user"
			case "wrong-namespace":
				kubernetes["namespace"] = "other"
			case "null-audience":
				claims["aud"] = nil
			}
			changed, err := json.Marshal(claims)
			if err != nil {
				t.Fatal(err)
			}
			token := parts[0] + "." + base64.RawURLEncoding.EncodeToString(changed) + ".do-not-echo"
			if test == "not-jwt" {
				token = "do-not-echo"
			} else if test == "bad-base64" {
				token = parts[0] + ".do-not-echo%.do-not-echo"
			}
			path := filepath.Join(t.TempDir(), "token")
			writeProjectedToken(t, path, token)
			c := InvocationCredentials{Tokens: &TokenSource{Path: path}, Authority: "factory", A2AEndpoint: "http://kagent", GRPCTarget: "kagent:8083"}
			_, identity, err := c.Pin(context.Background())
			if !errors.Is(err, ErrCallerIdentity) || identity.Valid() {
				t.Fatalf("invalid identity accepted: %+v, %v", identity, err)
			}
			if strings.Contains(err.Error(), "do-not-echo") || strings.Contains(err.Error(), token) {
				t.Fatal("identity error exposed the credential")
			}
		})
	}
}

func TestCallerIdentityRequiresConfiguredBoundary(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	writeProjectedToken(t, path, projectedTestToken(t, "https://cluster.example", "sa-1", "first"))
	for _, c := range []*InvocationCredentials{
		nil,
		{},
		{Tokens: &TokenSource{Path: "missing"}, Authority: "factory", A2AEndpoint: "http://kagent", GRPCTarget: "kagent:8083"},
		{Tokens: &TokenSource{Path: path}, A2AEndpoint: "http://kagent", GRPCTarget: "kagent:8083"},
		{Tokens: &TokenSource{Path: path}, Authority: "factory", GRPCTarget: "kagent:8083"},
		{Tokens: &TokenSource{Path: path}, Authority: "factory", A2AEndpoint: "http://kagent"},
	} {
		if _, _, err := c.Pin(context.Background()); !errors.Is(err, ErrCallerIdentity) {
			t.Fatalf("incomplete boundary accepted: %v", err)
		}
	}
}
