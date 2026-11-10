package store

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
)

const hillsFixture = `{"schema_version":1,"source":{"name":"DoBIH","version":"18.6","attribution":"The Database of British and Irish Hills v18.6","url":"https://www.hill-bagging.co.uk/dobih/","license":"https://creativecommons.org/licenses/by/4.0/","modifications":"Filtered to Scotland; joined to walks."},"hills":[{"number":1,"name":"Ben Nevis","classes":["Munro"],"lat":56.79685,"lon":-5.00351,"height_m":1345}]}`

func TestHillsAbsentConditionalAndFailure(t *testing.T) {
	f := &fakeR2{calls: map[string]int{}, objects: map[string]r2.Object{}, fail: r2.ErrNotFound}
	st := NewHills("stars", "hills.json", time.Minute, f, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx := context.Background()
	if err := st.Refresh(ctx); err == nil || st.Current() != nil {
		t.Fatal("missing snapshot loaded")
	}
	f.fail = nil
	f.objects["hills.json"] = r2.Object{Body: []byte(hillsFixture), ETag: "h1"}
	if err := st.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	first := st.Current()
	if err := st.Refresh(ctx); err != nil || st.Current() != first {
		t.Fatal("304 replaced working copy", err)
	}
	f.objects["hills.json"] = r2.Object{Body: []byte(`{}`), ETag: "bad"}
	if err := st.Refresh(ctx); err == nil || st.Current() != first || st.tag != "h1" {
		t.Fatal("bad upload replaced snapshot")
	}
	f.fail = r2.ErrNotFound
	if err := st.Refresh(ctx); err == nil || st.Current() != first {
		t.Fatal("404 replaced snapshot")
	}
	f.fail = nil
	f.objects["hills.json"] = r2.Object{Body: []byte(hillsFixture), ETag: "h2"}
	if err := st.Refresh(ctx); err != nil || st.Current() == first || st.Current().Version != "h2" {
		t.Fatal("replacement failed", err)
	}
}
