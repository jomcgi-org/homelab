package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"image/png"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strconv"
	"testing"
	"time"
)

func TestDelivery(t *testing.T) {
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
				if r.URL.Path != "/api/v1/query_range" || q.Get("query") != "up" || end-start != 21600 {
					t.Error("wrong query", q)
				}
				if tc.fail {
					w.WriteHeader(503)
					return
				}
				fmt.Fprintf(w, `{"status":"success","data":{"resultType":"matrix","result":[{"metric":{"job":"api"},"values":[[%d,"1"],[%d,"2"]]}]}}`, start, end)
			}))
			defer prom.Close()
			discord := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if err := r.ParseMultipartForm(1 << 20); err != nil {
					t.Error(err)
					return
				}
				var p struct{ Embeds []embed }
				if err := json.Unmarshal([]byte(r.FormValue("payload_json")), &p); err != nil {
					t.Error(err)
					return
				}
				want := embed{Title: "🔴 Queue stalled", Description: "Restart worker\n[Runbook](https://example.org/runbook)\nStarted <t:100:R>", Color: 0xe74c3c}
				if tc.status == "resolved" {
					want = embed{Title: "🟢 Queue stalled · resolved after 2m0s", Color: 0x57f287}
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
			a := alert{Status: tc.status, StartsAt: time.Unix(100, 0), EndsAt: time.Unix(220, 0), Labels: map[string]string{"severity": "critical"}, Annotations: map[string]string{"summary": "Queue stalled", "action": "Restart worker", "runbook": "https://example.org/runbook", "graph": "up"}}
			r := relay{http.DefaultClient, prom.URL, discord.URL}
			if err := r.deliver(context.Background(), a); err != nil {
				t.Fatal(err)
			}
			if queried != (tc.status == "firing") {
				t.Error("unexpected query count")
			}
		})
	}
}

func TestGraphGolden(t *testing.T) {
	got, err := render([]series{{"job=worker", []sample{{0, 2}, {9000, 5}, {21600, 3}}}, {"job=api", []sample{{0, 1}, {21600, 2}}}}, 0, 9000, 4)
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
