// Package testutil checks exporter transport options using in-memory connections.
package testutil

import (
	"context"
	"io"
	"net"
	"testing"
	"time"

	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"google.golang.org/grpc"
	"google.golang.org/grpc/resolver"
	"google.golang.org/grpc/resolver/manual"
)

// CheckEndpoint observes the destination and initial transport bytes without a collector.
func CheckEndpoint(t *testing.T, options []otlptracegrpc.Option, wantAddress string, wantTLS bool) {
	t.Helper()
	type attempt struct {
		address string
		first   byte
		err     error
	}
	observed := make(chan attempt, 8)
	nameResolver := manual.NewBuilderWithScheme("dns")
	nameResolver.BuildCallback = func(target resolver.Target, _ resolver.ClientConn, _ resolver.BuildOptions) {
		if got := target.Endpoint(); got != wantAddress {
			t.Errorf("resolver endpoint=%q, want %q", got, wantAddress)
		}
	}
	nameResolver.InitialState(resolver.State{Addresses: []resolver.Address{{Addr: wantAddress}}})
	options = append(options, otlptracegrpc.WithDialOption(grpc.WithResolvers(nameResolver), grpc.WithNoProxy(), grpc.WithContextDialer(func(ctx context.Context, address string) (net.Conn, error) {
		client, peer := net.Pipe()
		go func() {
			defer peer.Close()
			_ = peer.SetReadDeadline(time.Now().Add(time.Second))
			var data [1]byte
			_, err := io.ReadFull(peer, data[:])
			select {
			case observed <- attempt{address, data[0], err}:
			default:
			}
		}()
		return client, nil
	})))
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	exporter, err := otlptracegrpc.New(ctx, options...)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = exporter.Shutdown(context.Background()) }()
	done := make(chan error, 1)
	go func() {
		now := time.Now()
		done <- exporter.ExportSpans(ctx, []sdktrace.ReadOnlySpan{tracetest.SpanStub{Name: "transport-probe", StartTime: now, EndTime: now}.Snapshot()})
	}()
	select {
	case got := <-observed:
		if got.err != nil {
			t.Fatalf("read transport preface: %v", got.err)
		}
		if got.address != wantAddress {
			t.Errorf("address=%q, want %q", got.address, wantAddress)
		}
		want := byte('P')
		if wantTLS {
			want = 0x16
		} // HTTP/2 preface or TLS handshake record.
		if got.first != want {
			t.Errorf("transport first byte=%#x, want %#x (TLS=%t)", got.first, want, wantTLS)
		}
	case err := <-done:
		t.Fatal("export ended before in-memory dial:", err)
	case <-ctx.Done():
		t.Fatal("no in-memory dial attempt:", ctx.Err())
	}
	cancel()
	<-done
}
