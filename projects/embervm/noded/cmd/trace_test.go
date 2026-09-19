package main

import (
	"context"
	"net"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

type traceProbeServer struct {
	nodev1.UnimplementedNodeServiceServer
	contexts chan context.Context
}

func (s *traceProbeServer) Prime(ctx context.Context, _ *nodev1.PrimeRequest) (*nodev1.PrimeResponse, error) {
	s.contexts <- ctx
	return &nodev1.PrimeResponse{VmId: "vm-trace-probe"}, nil
}

func startTraceProbe(t *testing.T, bearerToken string) (nodev1.NodeServiceClient, <-chan context.Context) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	probe := &traceProbeServer{contexts: make(chan context.Context, 4)}
	server := grpc.NewServer(grpcServerOptions(bearerToken)...)
	nodev1.RegisterNodeServiceServer(server, probe)
	go func() { _ = server.Serve(lis) }()
	t.Cleanup(func() {
		server.Stop()
		_ = lis.Close()
	})

	dialCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(
		dialCtx,
		lis.Addr().String(),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		t.Fatalf("dial trace probe: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return nodev1.NewNodeServiceClient(conn), probe.contexts
}

func callTraceProbe(t *testing.T, client nodev1.NodeServiceClient, values ...string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs(values...))
	if _, err := client.Prime(ctx, &nodev1.PrimeRequest{}); err != nil {
		t.Fatalf("Prime: %v", err)
	}
}

func TestGRPCServerOptionsExtractTraceparentOnProductionPath(t *testing.T) {
	client, contexts := startTraceProbe(t, "node-secret")
	traceparent := "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	callTraceProbe(t, client,
		"authorization", "Bearer node-secret",
		"traceparent", traceparent,
		"x-request-id", "request-1",
	)

	ctx := <-contexts
	spanContext := trace.SpanContextFromContext(ctx)
	if got, want := spanContext.TraceID().String(), "4bf92f3577b34da6a3ce929d0e0e4736"; got != want {
		t.Fatalf("trace ID = %q, want %q", got, want)
	}
	if got, want := spanContext.SpanID().String(), "00f067aa0ba902b7"; got != want {
		t.Fatalf("parent span ID = %q, want %q", got, want)
	}
	if !spanContext.IsRemote() || !spanContext.IsSampled() {
		t.Fatalf("span context = %v, want remote sampled parent", spanContext)
	}
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok || len(md.Get("authorization")) != 1 || md.Get("authorization")[0] != "Bearer node-secret" ||
		len(md.Get("x-request-id")) != 1 || md.Get("x-request-id")[0] != "request-1" {
		t.Fatalf("incoming metadata = %v, want auth and unrelated metadata preserved", md)
	}
	if _, ok := ctx.Deadline(); !ok {
		t.Fatal("extracted RPC context lost its deadline")
	}
}

func TestGRPCServerOptionsHandleAbsentMalformedAndUnsampledContextWithoutLeakage(t *testing.T) {
	client, contexts := startTraceProbe(t, "")

	callTraceProbe(t, client, "traceparent", "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-00")
	unsampled := trace.SpanContextFromContext(<-contexts)
	if !unsampled.IsValid() || unsampled.IsSampled() || !unsampled.IsRemote() {
		t.Fatalf("unsampled context = %v, want valid remote unsampled context", unsampled)
	}

	callTraceProbe(t, client, "x-request-id", "request-without-trace")
	if got := trace.SpanContextFromContext(<-contexts); got.IsValid() {
		t.Fatalf("absent traceparent inherited prior request context: %v", got)
	}

	callTraceProbe(t, client, "traceparent", "not-a-traceparent", "x-request-id", "malformed")
	if got := trace.SpanContextFromContext(<-contexts); got.IsValid() {
		t.Fatalf("malformed traceparent produced a valid context: %v", got)
	}
}

func TestTraceContextInterceptorPreservesCancellation(t *testing.T) {
	base, cancel := context.WithCancel(context.Background())
	ctx := metadata.NewIncomingContext(base, metadata.Pairs(
		"traceparent", "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
	))

	handlerCalled := false
	_, err := unaryTraceContextInterceptor()(ctx, nil, &grpc.UnaryServerInfo{}, func(extracted context.Context, _ any) (any, error) {
		handlerCalled = true
		cancel()
		select {
		case <-extracted.Done():
			return nil, extracted.Err()
		case <-time.After(time.Second):
			t.Fatal("extracted context did not retain cancellation")
			return nil, nil
		}
	})
	if !handlerCalled || err != context.Canceled {
		t.Fatalf("handler called = %v, err = %v, want true and context canceled", handlerCalled, err)
	}
}
