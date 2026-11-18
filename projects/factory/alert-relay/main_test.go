package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image/png"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
)

func TestDelivery(t *testing.T) {
	const graphExpr = `max(factory_broker_grant_quota_exhausted{grant="codex+ops&review"})`
	for _, tc := range []struct {
		name, status string
		fail, graph  bool
	}{
		{"firing", "firing", false, true}, {"query error", "firing", true, false}, {"resolved", "resolved", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			queried := false
			prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				queried = true
				q := r.URL.Query()
				start, _ := strconv.Atoi(q.Get("start"))
				end, _ := strconv.Atoi(q.Get("end"))
				if r.URL.Path != "/api/v1/query_range" || q.Get("query") != graphExpr || end-start != 21600 {
					t.Error("wrong query", q)
				}
				if tc.fail {
					w.WriteHeader(503)
					return
				}
				fmt.Fprintf(w, `{"status":"success","data":{"resultType":"matrix","result":[{"metric":{"job":"api"},"values":[[%d,"1"],[%d,"2"]]}]}}`, start, end)
			}))
			defer prom.Close()
			var paths []string
			discord := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				paths = append(paths, r.URL.Path)
				if err := r.ParseMultipartForm(1 << 20); err != nil {
					t.Error(err)
					return
				}
				var p struct{ Embeds []embed }
				if err := json.Unmarshal([]byte(r.FormValue("payload_json")), &p); err != nil {
					t.Error(err)
					return
				}
				want := embed{Title: "🔴 Quota exhausted", Description: "Reset the grant\nPrimary resets at 14:30 UTC. Weekly resets Friday.\n[Runbook](https://example.org/runbook)\nStarted <t:100:R>", Color: 0xe74c3c}
				if tc.status == "resolved" {
					want = embed{Title: "🟢 Quota exhausted · resolved after 2m0s", Color: 0x57f287}
				}
				if tc.status == "firing" && len(p.Embeds) == 1 {
					want.URL = p.Embeds[0].URL
					link, err := url.Parse(want.URL)
					if err != nil || link.Scheme != "https" || link.Host != "factory-grafana.jomcgi.dev" || link.Path != "/explore" || link.Query().Get("schemaVersion") != "1" {
						t.Fatalf("invalid Explore link: %q", want.URL)
					}
					var panes map[string]struct {
						Datasource string
						Queries    []struct{ Expr string }
					}
					if err := json.Unmarshal([]byte(link.Query().Get("panes")), &panes); err != nil {
						t.Fatal(err)
					}
					pane := panes["ale"]
					if pane.Datasource != "prometheus" || len(pane.Queries) != 1 || pane.Queries[0].Expr != graphExpr {
						t.Fatalf("unexpected Explore query: %+v", pane)
					}
				}
				if tc.graph {
					want.Image = map[string]string{"url": "attachment://graph.png"}
				}
				if !reflect.DeepEqual(p.Embeds, []embed{want}) {
					t.Errorf("unexpected embeds %+v", p.Embeds)
				}
				f, h, err := r.FormFile("files[0]")
				if tc.graph {
					if err != nil {
						t.Error(err)
						return
					}
					defer f.Close()
					if _, err := png.Decode(f); err != nil || h.Filename != "graph.png" {
						t.Error("invalid attachment")
					}
				} else if err != http.ErrMissingFile {
					t.Error("unexpected attachment")
				}
			}))
			defer discord.Close()
			a := alert{Status: tc.status, StartsAt: time.Unix(100, 0), EndsAt: time.Unix(220, 0), Labels: map[string]string{"severity": "critical"}, Annotations: map[string]string{"summary": "Quota exhausted", "action": "Reset the grant", "description": "Primary resets at 14:30 UTC. Weekly resets Friday.", "runbook": "https://example.org/runbook", "graph": graphExpr}}
			file := t.TempDir() + "/webhook-url"
			r := relay{http.DefaultClient, prom.URL, file}
			if err := os.WriteFile(file, []byte(discord.URL+"/old"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := r.deliver(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(file, []byte(discord.URL+"/new"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := r.deliver(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(paths, []string{"/old", "/new"}) {
				t.Fatalf("webhook paths = %v", paths)
			}
			if queried != (tc.status == "firing") {
				t.Error("unexpected query count")
			}
		})
	}
}

func TestGraphGolden(t *testing.T) {
	got, err := render([]series{{"job=worker", []sample{{0, 2}, {60, 3}, {9000, 5}, {21600, 3}}}, {"job=api", []sample{{0, 1}, {21600, 2}}}}, 0, 9000, 4)
	if err != nil {
		t.Fatal(err)
	}
	if os.Getenv("UPDATE_GOLDEN") == "1" {
		if err := os.WriteFile("testdata/graph.png", got, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile("testdata/graph.png")
	if err != nil {
		t.Fatal(err)
	}
	a, err := png.Decode(bytes.NewReader(want))
	if err != nil {
		t.Fatal(err)
	}
	b, err := png.Decode(bytes.NewReader(got))
	if err != nil || !reflect.DeepEqual(a, b) {
		t.Fatal("graph differs from golden")
	}
}

func TestEmbedLimits(t *testing.T) {
	long := strings.Repeat("🔥", 4097)
	a := alert{Annotations: map[string]string{"summary": long, "action": long, "runbook": "https://example.org"}}
	for _, status := range []string{"firing", "resolved"} {
		a.Status = status
		e := message(a, false)
		if len(utf16.Encode([]rune(e.Title))) > 256 || len(utf16.Encode([]rune(e.Description))) > 4096 {
			t.Fatal("embed exceeds Discord limits")
		}
		if status == "resolved" && !strings.HasSuffix(e.Title, "resolved after 0s") {
			t.Fatal("lost duration")
		}
		if status == "firing" && !strings.Contains(e.Description, "[Runbook](https://example.org)\nStarted <t:") {
			t.Fatal("lost alert context")
		}
	}
}
