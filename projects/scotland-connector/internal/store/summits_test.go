package store

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/summits"
)

type summitGetter struct {
	calls     int
	body      []byte
	fail      bool
	unchanged bool
}

func (g *summitGetter) Get(_ context.Context, bucket, key, tag string, max int64) (r2.Object, error) {
	g.calls++
	if bucket != "stars" || key != "summits.json" || max != summits.MaxBytes {
		panic("wrong snapshot read")
	}
	if g.fail {
		return r2.Object{}, r2.ErrNotFound
	}
	return r2.Object{Body: g.body, ETag: "test-tag", NotModified: g.unchanged}, nil
}

func TestSummitStoreDisabledAndLastGood(t *testing.T) {
	body, e := os.ReadFile("../summits/testdata/summits.json")
	if e != nil {
		t.Fatal(e)
	}
	get := &summitGetter{body: body}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	off := NewSummits(false, "stars", "summits.json", time.Minute, get, log)
	if e = off.Refresh(context.Background()); e != nil {
		t.Fatal(e)
	}
	off.Run(context.Background())
	if get.calls != 0 || off.Current() != nil {
		t.Fatal("disabled store performed I/O")
	}
	on := NewSummits(true, "stars", "summits.json", time.Minute, get, log)
	get.unchanged = true
	if e = on.Refresh(context.Background()); e == nil {
		t.Fatal("304 without body")
	}
	get.unchanged = false
	if e = on.Refresh(context.Background()); e != nil {
		t.Fatal(e)
	}
	first := on.Current()
	for _, bad := range []string{`{`, strings.Replace(string(body), "2026-10-20T07:00:00Z", "2026-10-20T06:00:00Z", 1), strings.Replace(string(body), "2026-10-20T06:00:00Z", "2026-10-20T05:00:00Z", 1)} {
		get.body = []byte(bad)
		if e = on.Refresh(context.Background()); e == nil || on.Current() != first {
			t.Fatal("bad replacement accepted", e)
		}
	}
	get.fail = true
	if e = on.Refresh(context.Background()); e == nil || on.Current() != first {
		t.Fatal("outage changed data")
	}
	get.fail = false
	get.unchanged = true
	if e = on.Refresh(context.Background()); e != nil || on.Current() != first {
		t.Fatal("304 changed data", e)
	}
}
