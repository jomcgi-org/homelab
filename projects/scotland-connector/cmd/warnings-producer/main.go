// Command warnings-producer emits one warnings.json document to stdout. Storage
// publication is deliberately staged outside the public connector and this job.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings"
)

func main() {
	collect := flag.Bool("collect", false, "explicitly fetch the six public RSS feeds")
	flag.Parse()
	if !*collect {
		fmt.Fprintln(os.Stderr, "collection disabled; pass --collect")
		os.Exit(2)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Second)
	defer cancel()
	s := warnings.Collect(ctx, nil, time.Now)
	body, e := json.Marshal(s)
	if e == nil && len(body) > warnings.MaxBytes {
		e = fmt.Errorf("warnings snapshot exceeds %d bytes", warnings.MaxBytes)
	}
	if e == nil {
		_, e = os.Stdout.Write(append(body, byte('\n')))
	}
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
	// Partial collection is still a valid snapshot, visibly incomplete. The job
	// fails so a future publisher cannot mistake it for complete collection.
	for _, r := range s.Regions {
		if !r.Complete {
			fmt.Fprintln(os.Stderr, "incomplete region", r.Code)
			os.Exit(1)
		}
	}
}
