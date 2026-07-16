package server

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	cachev3 "github.com/envoyproxy/go-control-plane/pkg/cache/v3"
	resourcev3 "github.com/envoyproxy/go-control-plane/pkg/resource/v3"

	"github.com/jomcgi/homelab/projects/embervm/xds/snapshot"
)

// maxBodyBytes caps a snapshot PUT. A desired-state document is a handful of
// clusters and routes; anything larger is malformed, so the read is bounded to
// avoid an unbounded-body memory spike on a localhost-only endpoint.
const maxBodyBytes = 1 << 20 // 1 MiB

// NewHTTPHandler builds the localhost snapshot API:
//
//	PUT  /snapshot/{envoy_node_id}  install a full desired-state document
//	GET  /snapshot/{envoy_node_id}  report what is currently served (debug)
//	GET  /healthz                   liveness/readiness
//
// The handler is bound to 127.0.0.1 by the caller (cmd): it is the control-plane
// container's private write channel to its own sidecar and must never be exposed
// on the pod network or the embervm Service.
func NewHTTPHandler(store *Store) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "ok\n")
	})
	mux.HandleFunc("/snapshot/", func(w http.ResponseWriter, r *http.Request) {
		node := strings.TrimPrefix(r.URL.Path, "/snapshot/")
		if node == "" || strings.Contains(node, "/") {
			writeError(w, http.StatusBadRequest, "envoy node id path segment is required")
			return
		}
		switch r.Method {
		case http.MethodPut:
			handlePut(w, r, store, node)
		case http.MethodGet:
			handleGet(w, r, store, node)
		default:
			w.Header().Set("Allow", "GET, PUT")
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		}
	})
	return mux
}

// handlePut decodes the desired-state document and installs it. A malformed body
// or a document that fails validation/consistency is a 400; a version that is not
// strictly greater than the currently served one is a 409 (well-formed but out of
// order); a successful swap is 200 with the applied version.
func handlePut(w http.ResponseWriter, r *http.Request, store *Store, node string) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	if len(body) > maxBodyBytes {
		writeError(w, http.StatusRequestEntityTooLarge, "desired-state document too large")
		return
	}

	var d snapshot.Desired
	dec := json.NewDecoder(strings.NewReader(string(body)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&d); err != nil {
		writeError(w, http.StatusBadRequest, "decode desired state: "+err.Error())
		return
	}

	if err := store.Apply(r.Context(), node, &d); err != nil {
		switch {
		case IsVersionConflict(err):
			writeError(w, http.StatusConflict, err.Error())
		default:
			writeError(w, http.StatusBadRequest, err.Error())
		}
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"node":     node,
		"version":  d.Version,
		"clusters": len(d.Clusters),
		"routes":   len(d.Routes),
	})
}

// servedSnapshot is the debug view of what the cache serves for a node: the
// version plus the resource names per xDS type. It deliberately does NOT dump the
// full proto (heavy and unstable); names + counts are enough to confirm a push
// landed and to correlate with Envoy's config_dump.
type servedSnapshot struct {
	Node      string   `json:"node"`
	Version   string   `json:"version"`
	Clusters  []string `json:"clusters"`
	Endpoints []string `json:"endpoints"`
	Routes    []string `json:"routes"`
}

// handleGet reports the snapshot currently served for a node. A node with no
// applied snapshot yet is a 404 (nothing is served), distinct from an error.
func handleGet(w http.ResponseWriter, _ *http.Request, store *Store, node string) {
	raw, err := store.Cache().GetSnapshot(node)
	if err != nil {
		writeError(w, http.StatusNotFound, "no snapshot served for node "+node)
		return
	}
	snap, ok := raw.(*cachev3.Snapshot)
	if !ok {
		writeError(w, http.StatusInternalServerError, "unexpected snapshot type")
		return
	}
	version, _ := store.CurrentVersion(node)
	writeJSON(w, http.StatusOK, servedSnapshot{
		Node:      node,
		Version:   version,
		Clusters:  resourceNames(snap, resourcev3.ClusterType),
		Endpoints: resourceNames(snap, resourcev3.EndpointType),
		Routes:    resourceNames(snap, resourcev3.RouteType),
	})
}

// resourceNames lists the resource names of one xDS type in a snapshot, sorted by
// the cache's own map iteration (order is not meaningful, names are).
func resourceNames(snap *cachev3.Snapshot, typeURL resourcev3.Type) []string {
	res := snap.GetResources(typeURL)
	names := make([]string, 0, len(res))
	for name := range res {
		names = append(names, name)
	}
	return names
}

type errorResponse struct {
	Error string `json:"error"`
}

func writeError(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, errorResponse{Error: msg})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil && !errors.Is(err, io.ErrClosedPipe) {
		// The response header is already committed; a body encode failure can only
		// be logged by the caller's server error log, not signaled to the client.
		_ = err
	}
}
