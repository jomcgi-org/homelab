package reaper

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"google.golang.org/protobuf/encoding/protowire"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

func TestSuspendDeletionScope(t *testing.T) {
	now := time.Now()
	p := placement{"factory-r-homelab", "homelab-codex-abcdef1234-abcde", "original"}
	old := now.Add(-6 * time.Minute)
	if !suspendExpired(p, 3, old, now) || suspendExpired(p, 2, old, now) ||
		suspendExpired(p, 3, now.Add(-5*time.Minute), now) {
		t.Fatal("incorrect suspend deadline")
	}
	for _, other := range []placement{
		{"factory", p.pod, p.uid}, {p.namespace, "other", p.uid}, {p.namespace, p.pod, ""},
	} {
		if suspendExpired(other, 3, old, now) {
			t.Fatalf("eligible for deletion: %+v", other)
		}
	}
}

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

// Encode the pinned Actor message, including metadata.update_time and the
// status.worker_assignment submessage. The scan reads this from PostgreSQL.
func actorFixture(uid string, state uint64, updated time.Time) []byte {
	field := func(n protowire.Number, value []byte) []byte {
		return protowire.AppendBytes(protowire.AppendTag(nil, n, protowire.BytesType), value)
	}
	number := func(n protowire.Number, value uint64) []byte {
		return protowire.AppendVarint(protowire.AppendTag(nil, n, protowire.VarintType), value)
	}
	worker := field(1, []byte("factory-r-homelab"))
	worker = append(worker, field(3, []byte("homelab-codex-worker"))...)
	worker = append(worker, field(4, []byte(uid))...)
	status := append(number(1, state), field(2, worker)...)
	metadata := append(field(3, []byte("actor-incarnation")), number(4, 19)...)
	metadata = append(metadata, field(6, number(1, uint64(updated.Unix())))...)
	return append(field(1, metadata), field(5, status)...)
}

func TestScanDeletesOnlyCurrentSuspendingRevision(t *testing.T) {
	for _, scenario := range []string{"stalled", "fresh", "reassigned"} {
		changed := scenario == "reassigned"
		updated := time.Now().Add(-6 * time.Minute)
		if scenario == "fresh" {
			updated = time.Now()
		}
		db, err := sql.Open("postgres", pgtest.NewDB(t))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { db.Close() })
		_, err = db.Exec(`CREATE TABLE revisions (id int PRIMARY KEY, proto bytea);
			CREATE SEQUENCE reads;
			CREATE FUNCTION public.reaper_actors() RETURNS TABLE(name text, proto bytea)
			LANGUAGE sql VOLATILE AS $$SELECT 'session-held'::text,r.proto FROM revisions r
			WHERE r.id=(SELECT least(nextval('reads'),2))$$`)
		if err != nil {
			t.Fatal(err)
		}
		first := actorFixture("original", 3, updated)
		second := first
		if changed {
			second = actorFixture("replacement", 3, updated)
		}
		if _, err = db.Exec(`INSERT INTO revisions VALUES (1,$1),(2,$2)`, first, second); err != nil {
			t.Fatal(err)
		}
		requests := make(chan bool, 2)
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			var opts metav1.DeleteOptions
			err := json.NewDecoder(r.Body).Decode(&opts)
			requests <- r.Method == "DELETE" && r.URL.Path == "/api/v1/namespaces/factory-r-homelab/pods/homelab-codex-worker" && err == nil && opts.Preconditions != nil && opts.Preconditions.UID != nil && *opts.Preconditions.UID == "original"
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Success"}`))
		}))
		t.Cleanup(server.Close)
		client, err := core.NewForConfig(&rest.Config{Host: server.URL, ContentConfig: rest.ContentConfig{ContentType: "application/json"}})
		if err != nil {
			t.Fatal(err)
		}
		if err := Scan(context.Background(), db, db, client); err != nil {
			t.Fatal(err)
		}
		if scenario != "stalled" && len(requests) != 0 {
			t.Fatal("deleted after assignment changed")
		}
		if scenario == "stalled" && (len(requests) != 1 || !<-requests) {
			t.Fatal("expected one UID-preconditioned DELETE")
		}
	}
}

// A busy turn keeps its original UID until the API reports it gone. Recheck
// the actor after the 404: a new assignment invalidates that observation.
func TestScanWorkerDisappearance(t *testing.T) {
	db, err := sql.Open("postgres", pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	_, err = db.Exec(`CREATE TABLE actor_fixture(proto bytea);
	CREATE FUNCTION public.reaper_actors() RETURNS TABLE(name text, proto bytea)
	LANGUAGE sql AS $$ SELECT 'session-held'::text, proto FROM actor_fixture $$;
	INSERT INTO run(id,agent_key,trigger,prompt,system_prompt,attempt,state) VALUES(1,'coder','issue','','',1,'dispatched');
	INSERT INTO delivery_attempt(run_id,seq,run_attempt,work_item_epoch,decision_id,kind,message_id,kagent_namespace,kagent_agent,context_id,state,writer_epoch)
	VALUES(1,1,1,1,'decision','initial','msg','ns','coder','held','accepted',1)`)
	if err != nil {
		t.Fatal(err)
	}
	running := func(uid string) []byte { return actorFixture(uid, 2, time.Now()) }
	if _, err = db.Exec("INSERT INTO actor_fixture VALUES($1)", running("original")); err != nil {
		t.Fatal(err)
	}
	for _, replacement := range []bool{true, false} {
		if _, err = db.Exec("UPDATE actor_fixture SET proto=$1", running("original")); err != nil {
			t.Fatal(err)
		}
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method != "GET" {
				t.Errorf("unexpected method %s", r.Method)
			}
			if replacement {
				if _, err := db.Exec("UPDATE actor_fixture SET proto=$1", running("replacement")); err != nil {
					t.Error(err)
				}
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound","code":404}`))
		}))
		client, err := core.NewForConfig(&rest.Config{Host: server.URL})
		if err != nil {
			t.Fatal(err)
		}
		err = Scan(context.Background(), db, db, client)
		server.Close()
		if err != nil {
			t.Fatal(err)
		}
		var gone bool
		if err = db.QueryRow("SELECT gone_at IS NOT NULL AND actor_uid='actor-incarnation' AND actor_version=19 FROM worker_fence WHERE session_id='held' AND uid='original'").Scan(&gone); err != nil || gone == replacement {
			t.Fatalf("replacement=%t gone=%t err=%v", replacement, gone, err)
		}
	}
}
