package driver

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func TestCopyProvisionerCopiesBase(t *testing.T) {
	base := filepath.Join(shortTempDir(t), "base.ext4")
	if err := os.WriteFile(base, []byte("ROOTFS-BYTES"), 0o600); err != nil {
		t.Fatalf("write base: %v", err)
	}
	dir := shortTempDir(t)

	p := &CopyProvisioner{Base: base}
	got, err := p.Provision(context.Background(), "t1", dir)
	if err != nil {
		t.Fatalf("Provision: %v", err)
	}
	if got != filepath.Join(dir, "rootfs.ext4") {
		t.Fatalf("rootfs path = %q", got)
	}
	b, err := os.ReadFile(got)
	if err != nil {
		t.Fatalf("read provisioned: %v", err)
	}
	if string(b) != "ROOTFS-BYTES" {
		t.Fatalf("provisioned content = %q, want the base bytes", b)
	}
}

func TestCopyProvisionerEmptyBaseErrors(t *testing.T) {
	if _, err := (&CopyProvisioner{}).Provision(context.Background(), "t1", t.TempDir()); err == nil {
		t.Fatal("empty Base should error")
	}
}

// fakeProvisioner records calls and creates a stub rootfs file.
type fakeProvisioner struct {
	calls    atomic.Int32
	threadID string
	tornDown string
	fail     error
}

func (f *fakeProvisioner) Provision(_ context.Context, threadID, dir string) (string, error) {
	f.calls.Add(1)
	f.threadID = threadID
	if f.fail != nil {
		return "", f.fail
	}
	path := filepath.Join(dir, "rootfs.ext4")
	_ = os.WriteFile(path, []byte("stub"), 0o600)
	return path, nil
}

func (f *fakeProvisioner) Teardown(_ context.Context, threadID string) error {
	f.tornDown = threadID
	return nil
}

func TestDriverClaimProvisionsPerThreadRootfs(t *testing.T) {
	d := testDriver(t)
	fp := &fakeProvisioner{}
	d.SetProvisioner(fp)

	h, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "t-prov"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	if fp.calls.Load() != 1 {
		t.Fatalf("provisioner called %d times, want 1", fp.calls.Load())
	}
	if fp.threadID != "t-prov" {
		t.Fatalf("provisioned for %q, want t-prov", fp.threadID)
	}
	if h.ThreadID != "t-prov" {
		t.Fatalf("handle thread = %q", h.ThreadID)
	}
}

func TestColdBootSpansContinueRemoteRPCParent(t *testing.T) {
	exporter := tracetest.NewInMemoryExporter()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	previousProvider := otel.GetTracerProvider()
	otel.SetTracerProvider(provider)
	t.Cleanup(func() {
		_ = provider.Shutdown(context.Background())
		otel.SetTracerProvider(previousProvider)
	})

	traceID, err := trace.TraceIDFromHex("4bf92f3577b34da6a3ce929d0e0e4736")
	if err != nil {
		t.Fatalf("parse trace ID: %v", err)
	}
	parentSpanID, err := trace.SpanIDFromHex("00f067aa0ba902b7")
	if err != nil {
		t.Fatalf("parse span ID: %v", err)
	}
	parent := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    traceID,
		SpanID:     parentSpanID,
		TraceFlags: trace.FlagsSampled,
		Remote:     true,
	})
	ctx := trace.ContextWithRemoteSpanContext(context.Background(), parent)

	d := testDriver(t)
	d.SetProvisioner(&fakeProvisioner{})
	if _, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-traced"}); err != nil {
		t.Fatalf("Claim: %v", err)
	}

	spansByName := make(map[string]tracetest.SpanStub)
	for _, span := range exporter.GetSpans() {
		spansByName[span.Name] = span
	}
	for _, name := range []string{"provision_rootfs", "firecracker_boot"} {
		span, ok := spansByName[name]
		if !ok {
			t.Fatalf("missing %q span; got %v", name, exporter.GetSpans())
		}
		if span.SpanContext.TraceID() != traceID {
			t.Errorf("%s trace ID = %s, want %s", name, span.SpanContext.TraceID(), traceID)
		}
		if span.Parent.SpanID() != parentSpanID || !span.Parent.IsRemote() {
			t.Errorf("%s parent = %v, want remote span %s", name, span.Parent, parentSpanID)
		}
	}
}

func TestDriverClaimProvisionFailureAborts(t *testing.T) {
	d := testDriver(t)
	d.SetProvisioner(&fakeProvisioner{fail: errors.New("no space")})
	if _, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "t1"}); err == nil {
		t.Fatal("Claim should fail when rootfs provisioning fails")
	}
	if d.LiveCount() != 0 {
		t.Fatalf("a failed provision should not leave a live VM; LiveCount=%d", d.LiveCount())
	}
}

func TestBootArgsAppendsInit(t *testing.T) {
	d := New(Config{
		KernelBootArgs: "console=ttyS0",
		HarnessInit:    "/usr/local/bin/fc-agent-init",
		SnapshotRoot:   shortTempDir(t),
		Node:           "node-4", Arch: "amd64",
	}, &fakeLauncher{}, nil)
	if got := d.bootArgs(); got != "console=ttyS0 init=/usr/local/bin/fc-agent-init" {
		t.Fatalf("bootArgs = %q", got)
	}
}
