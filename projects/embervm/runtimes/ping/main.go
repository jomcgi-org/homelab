// Command ping is the minimal HTTP guest used to prove the EmberVM serving
// path. It has no outbound dependencies and serves only health and identity.
package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"time"
)

const listenAddress = ":8080"

func main() {
	hostname, err := os.Hostname()
	if err != nil {
		log.Fatalf("read hostname: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = fmt.Fprintf(w, "embervm ping from %s at %s\n", hostname, time.Now().UTC().Format(time.RFC3339Nano))
	})

	log.Printf("embervm ping listening on %s", listenAddress)
	if err := http.ListenAndServe(listenAddress, mux); err != nil {
		log.Fatal(err)
	}
}
