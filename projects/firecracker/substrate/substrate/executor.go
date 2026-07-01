package substrate

import (
	"context"
	"io"
	"net/http"
)

// NodeExecutor is the control-plane to data-plane seam (ADR 031). The cluster
// ingress routes an invocation to the NodeExecutor registered for a workload,
// which runs it on a node: an in-process local executor today (node/invoker), a
// remote node daemon over gRPC/HTTP once the planes split across processes. It
// lives in this neutral seam package so the cluster plane depends on the
// interface and the node plane implements it without either importing the other.
//
// A returned error whose chain implements GuestUnavailable() bool means no guest
// could be obtained (the ingress maps it to 503); any other error means the
// guest ran but the HTTP round-trip failed (mapped to 502).
type NodeExecutor interface {
	Invoke(ctx context.Context, session string, body io.Reader) (*http.Response, error)
}
