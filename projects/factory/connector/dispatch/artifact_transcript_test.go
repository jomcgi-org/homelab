package dispatch_test

import (
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/factory/connector/dispatch"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func newArtifactEnv(t *testing.T) *env {
	t.Helper()
	v := newEnv(t)
	v.e.Release()
	v.cfg.ArtifactTranscripts = true
	v.e = v.engine("artifact-writer")
	return v
}

func textArtifact(id string, parts ...string) *kagent.Artifact {
	a := &kagent.Artifact{ArtifactID: id}
	for _, p := range parts {
		a.Parts = append(a.Parts, kagent.Part{Text: p})
	}
	return a
}

// Native completion changes status without adding an assistant history entry.
func artifactSnapshot(v *env, taskID, state string, artifacts ...*kagent.Artifact) {
	v.kg.mu.Lock()
	defer v.kg.mu.Unlock()
	task := &v.kg.tasks[taskID].task
	task.Status = kagent.TaskStatus{State: state}
	task.Artifacts = artifacts
}

func assertTranscript(t *testing.T, events []itsaplan.Event, wantText []string, ended bool) {
	t.Helper()
	var ids []string
	text := map[string]string{}
	ends := map[string]bool{}
	for _, ev := range events {
		if ev.MessageID == "" || len(ev.MessageID) > 200 {
			t.Fatalf("invalid message ID %q", ev.MessageID)
		}
		switch ev.Type {
		case "TEXT_MESSAGE_START":
			if _, exists := text[ev.MessageID]; exists || ev.Role != "assistant" {
				t.Fatalf("invalid or repeated START: %+v", ev)
			}
			ids = append(ids, ev.MessageID)
			text[ev.MessageID] = ""
		case "TEXT_MESSAGE_CONTENT":
			if _, exists := text[ev.MessageID]; !exists || ends[ev.MessageID] {
				t.Fatalf("CONTENT outside message: %+v", ev)
			}
			if ev.Delta == "" || len(utf16.Encode([]rune(ev.Delta))) > 12000 || !utf8.ValidString(ev.Delta) {
				t.Fatalf("invalid delta length/encoding: %d bytes", len(ev.Delta))
			}
			text[ev.MessageID] += ev.Delta
		case "TEXT_MESSAGE_END":
			if _, exists := text[ev.MessageID]; !exists || ends[ev.MessageID] {
				t.Fatalf("invalid or repeated END: %+v", ev)
			}
			ends[ev.MessageID] = true
		default:
			t.Fatalf("unexpected event: %+v", ev)
		}
	}
	if len(ids) != len(wantText) {
		t.Fatalf("messages = %d, want %d", len(ids), len(wantText))
	}
	for i, id := range ids {
		if text[id] != wantText[i] {
			t.Errorf("message %d = %q, want %q", i, text[id], wantText[i])
		}
		if ends[id] != ended {
			t.Errorf("message %d ended = %t, want %t", i, ends[id], ended)
		}
	}
}

func TestNativeArtifactTranscriptGrowsAcrossRestart(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	artifactSnapshot(v, id, kagent.StateWorking, textArtifact("text", "hel"))
	v.tick(v.e)
	first := append([]itsaplan.Event(nil), v.its.Run(1).events[1]...)
	assertTranscript(t, first, []string{"hel"}, false)
	if len(first) != 2 {
		t.Fatalf("partial snapshot produced %d events, want START and CONTENT", len(first))
	}

	v.e.Release()
	v.e = v.engine("restarted")
	artifactSnapshot(v, id, kagent.StateWorking, textArtifact("text", "hel", "lo 🌍"))
	v.tick(v.e)
	grown := append([]itsaplan.Event(nil), v.its.Run(1).events[1]...)
	assertTranscript(t, grown, []string{"hello 🌍"}, false)
	if !reflect.DeepEqual(grown[:len(first)], first) || len(grown) != 3 || grown[2].Delta != "lo 🌍" {
		t.Fatalf("growth moved existing events or repeated text: %+v", grown)
	}
	v.tick(v.e)
	if !reflect.DeepEqual(v.its.Run(1).events[1], grown) {
		t.Fatal("unchanged snapshot appended events")
	}

	artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("text", "hello 🌍!"))
	v.tick(v.e)
	fr := v.its.Run(1)
	assertTranscript(t, fr.events[1], []string{"hello 🌍!"}, true)
	if len(fr.results) != 1 || fr.results[0].Output != "hello 🌍!" {
		t.Fatalf("native final output = %+v", fr.results)
	}
}

func TestArtifactTranscriptReplaysDurableEventsAfterLostAcknowledgement(t *testing.T) {
	for _, accepted := range []bool{false, true} {
		t.Run(fmt.Sprintf("accepted=%t", accepted), func(t *testing.T) {
			v := newArtifactEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			id := v.kg.lastTask()
			artifactSnapshot(v, id, kagent.StateWorking, textArtifact("text", "before"))
			if accepted {
				v.its.loseEventAck = true
			} else {
				v.its.eventErr = errors.New("request did not arrive")
			}
			v.tick(v.e)
			if testutil.ToFloat64(v.m.TickErrors.WithLabelValues("runs")) != 1 {
				t.Fatal("event transport failure was not recorded")
			}
			if v.run(1).EventOffset != 0 {
				t.Fatal("lost acknowledgement advanced the local delivery offset")
			}
			journal, err := v.st.GetTranscript(v.ctx, v.st.DB, 1, 1, id)
			if err != nil || journal == nil {
				t.Fatalf("events were sent before journaling: %v", err)
			}
			v.its.eventErr = nil
			v.e.Release()
			v.e = v.engine("restarted")
			artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("text", "before and after"))
			v.tick(v.e)
			events := v.its.Run(1).events[1]
			assertTranscript(t, events, []string{"before and after"}, true)
			if len(events) != 4 || events[1].Delta != "before" || events[2].Delta != " and after" {
				t.Fatalf("replay recomputed persisted event boundaries: %+v", events)
			}
			if accepted && testutil.ToFloat64(v.m.Events.WithLabelValues("conflict")) != 1 {
				t.Fatal("growing snapshot did not exercise offset-conflict recovery")
			}
		})
	}
}

func TestMultipleArtifactCursorsKeepIdentityAndChunkBoundaries(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	// These IDs would collide if clipped to itsaplan's 200 byte limit.
	a, b := strings.Repeat("a", 210)+"1", strings.Repeat("a", 210)+"2"
	long := strings.Repeat("界", 4001)
	artifactSnapshot(v, id, kagent.StateWorking, nil,
		&kagent.Artifact{ArtifactID: "tool", Parts: []kagent.Part{{Data: map[string]any{"tool": "read"}}}},
		textArtifact(a, long), textArtifact(b, "second"), textArtifact("empty", ""))
	v.tick(v.e)
	first := append([]itsaplan.Event(nil), v.its.Run(1).events[1]...)
	assertTranscript(t, first, []string{long, "second"}, false)
	if len(first) != 5 {
		t.Fatalf("chunked events = %d, want 5", len(first))
	}
	v.e.Release()
	v.e = v.engine("restarted")
	// Array order and part boundaries do not identify an artifact. Both grow.
	artifactSnapshot(v, id, kagent.StateCompleted,
		textArtifact(b, "second", "!"), textArtifact(a, long+"!"), textArtifact("empty", "third"))
	v.tick(v.e)
	final := v.its.Run(1).events[1]
	assertTranscript(t, final, []string{long + "!", "second!", "third"}, true)
	if !reflect.DeepEqual(final[:len(first)], first) || final[5].MessageID != first[3].MessageID ||
		final[6].MessageID != first[0].MessageID {
		t.Fatal("artifact reorder changed event identities or acknowledged prefix")
	}
}

func TestArtifactTranscriptAcrossBatches(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	var artifacts []*kagent.Artifact
	var want []string
	for i := range 105 {
		text := fmt.Sprintf("artifact %d", i)
		artifacts = append(artifacts, textArtifact(text, text))
		want = append(want, text)
	}
	artifactSnapshot(v, id, kagent.StateWorking, artifacts...)
	v.its.loseEventAck = true
	v.tick(v.e)
	if testutil.ToFloat64(v.m.TickErrors.WithLabelValues("runs")) != 1 {
		t.Fatal("expected lost acknowledgement to fail the runs phase")
	}
	if got := len(v.its.Run(1).events[1]); got != 200 {
		t.Fatalf("first batch = %d, want 200", got)
	}
	v.e.Release()
	v.e = v.engine("restarted")
	artifactSnapshot(v, id, kagent.StateCompleted, artifacts...)
	v.tick(v.e)
	assertTranscript(t, v.its.Run(1).events[1], want, true)
}

func TestArtifactTranscriptResetsOnTransportRedelivery(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	artifactSnapshot(v, id, kagent.StateWorking, textArtifact("text", "partial"))
	v.tick(v.e)
	v.e.Release()
	v.clk.Advance(301 * time.Second)
	v.e = v.engine("restarted")
	// Claim before the first heartbeat can renew the old lease.
	again, err := v.its.Claim(v.ctx, "coder")
	if err != nil || again == nil || again.Attempts != 2 {
		t.Fatalf("redelivery = %+v, %v", again, err)
	}
	if err := v.e.HandleClaim(v.ctx, "coder", again); err != nil {
		t.Fatal(err)
	}
	artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("text", "partial then final"))
	v.tick(v.e)
	fr := v.its.Run(1)
	assertTranscript(t, fr.events[1], []string{"partial"}, false)
	assertTranscript(t, fr.events[2], []string{"partial then final"}, true)
	if v.kg.Executions() != 1 || len(fr.results) != 1 || fr.results[0].Attempt != 2 {
		t.Fatalf("redelivery did not attach the same task: %+v", fr.results)
	}
}

func TestArtifactTranscriptRejectsChangedPrefix(t *testing.T) {
	for _, replacement := range []string{"par", "replaced text"} {
		t.Run(replacement, func(t *testing.T) {
			v := newArtifactEnv(t)
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			id := v.kg.lastTask()
			artifactSnapshot(v, id, kagent.StateWorking, textArtifact("text", "partial"))
			v.tick(v.e)
			before := append([]itsaplan.Event(nil), v.its.Run(1).events[1]...)
			artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("text", replacement))
			v.tick(v.e)
			if testutil.ToFloat64(v.m.TickErrors.WithLabelValues("runs")) != 1 {
				t.Fatal("changed prefix was not rejected")
			}
			if !reflect.DeepEqual(v.its.Run(1).events[1], before) || len(v.its.Run(1).results) != 0 {
				t.Fatal("changed prefix advanced the transcript or finished the run")
			}
			artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("text", "partial restored"))
			v.tick(v.e)
			assertTranscript(t, v.its.Run(1).events[1], []string{"partial restored"}, true)
		})
	}
}

func TestArtifactTranscriptAdoptsLegacyHistoryOffset(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	v.kg.say(id, "history")
	// Model an upgrade with history already acknowledged by the old connector.
	if _, err := v.st.DB.ExecContext(v.ctx, `DELETE FROM task_transcript WHERE run_id = 1`); err != nil {
		t.Fatal(err)
	}
	task, err := v.kg.GetTask(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, id)
	if err != nil {
		t.Fatal(err)
	}
	history := dispatch.Events(task)
	if _, err := v.its.Events(v.ctx, "coder", 1, 1, 0, history); err != nil {
		t.Fatal(err)
	}
	if err := v.st.SetEventOffset(v.ctx, 1, 1, len(history), id, 0); err != nil {
		t.Fatal(err)
	}
	artifactSnapshot(v, id, kagent.StateWorking, textArtifact("text", "artifact"))
	v.tick(v.e)
	v.kg.say(id, "later history")
	artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("text", "artifact final"))
	v.tick(v.e)
	events := v.its.Run(1).events[1]
	assertTranscript(t, events, []string{"history", "artifact final", "later history"}, true)
	if !reflect.DeepEqual(events[:len(history)], history) {
		t.Fatal("upgrade changed previously acknowledged history events")
	}
}

func TestArtifactTranscriptRetryKeepsBaseAfterLostFinalAcknowledgement(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	first := v.kg.lastTask()
	artifactSnapshot(v, first, kagent.StateFailed, textArtifact("text", "failed attempt"))
	v.tick(v.e)
	second := v.kg.lastTask()
	if second == first {
		t.Fatal("engineering retry did not open a new task")
	}
	artifactSnapshot(v, second, kagent.StateCompleted, textArtifact("text", "retry finished"))
	v.its.loseEventAck = true
	v.tick(v.e)
	if r := v.run(1); r.EventOffset != 3 || r.EventTaskID != first {
		t.Fatalf("lost acknowledgement advanced the previous task's offset: %+v", r)
	}
	if len(v.its.Run(1).results) != 0 {
		t.Fatal("result overtook unacknowledged final transcript")
	}
	v.e.Release()
	v.e = v.engine("restarted")
	v.tick(v.e)
	fr := v.its.Run(1)
	assertTranscript(t, fr.events[1], []string{"failed attempt", "retry finished"}, true)
	if r := v.run(1); r.EventTaskBase != 3 || r.EventOffset != 6 {
		t.Fatalf("retry base changed after replay: %+v", r)
	}
	if len(fr.results) != 1 || fr.results[0].Output != "retry finished" {
		t.Fatalf("retry completion = %+v", fr.results)
	}
}

func TestArtifactTranscriptRejectsOmittedArtifacts(t *testing.T) {
	for _, state := range []string{kagent.StateWorking, kagent.StateCompleted} {
		for _, missingAll := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/all=%t", state, missingAll), func(t *testing.T) {
				v := newArtifactEnv(t)
				v.its.add(1, "coder", 101, "HL-1", "Do it.")
				v.tick(v.e)
				id := v.kg.lastTask()
				artifactSnapshot(v, id, kagent.StateWorking, textArtifact("a", "first"), textArtifact("b", "second"))
				v.tick(v.e)
				before := append([]itsaplan.Event(nil), v.its.Run(1).events[1]...)
				journal, err := v.st.GetTranscript(v.ctx, v.st.DB, 1, 1, id)
				if err != nil || journal == nil {
					t.Fatalf("read journal: %v", err)
				}
				artifacts := []*kagent.Artifact{nil}
				if !missingAll {
					artifacts = append(artifacts, textArtifact("a", "first grows"))
				}
				artifactSnapshot(v, id, state, artifacts...)
				v.tick(v.e)
				after, err := v.st.GetTranscript(v.ctx, v.st.DB, 1, 1, id)
				if err != nil || after == nil || string(after.State) != string(journal.State) {
					t.Fatalf("incomplete snapshot changed journal: %v", err)
				}
				if testutil.ToFloat64(v.m.TickErrors.WithLabelValues("runs")) != 1 ||
					!reflect.DeepEqual(v.its.Run(1).events[1], before) || len(v.its.Run(1).results) != 0 {
					t.Fatal("omitted artifact advanced events or completion")
				}
				v.e.Release()
				v.e = v.engine("restarted")
				artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("a", "first grows"), textArtifact("b", "second restored"))
				v.tick(v.e)
				assertTranscript(t, v.its.Run(1).events[1], []string{"first grows", "second restored"}, true)
			})
		}
	}
}

func TestArtifactTranscriptRetainsNULAndDeliversSupportedText(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	artifactSnapshot(v, id, kagent.StateWorking, textArtifact("a", "before\x00after"))
	v.its.loseEventAck = true
	v.tick(v.e)
	journal, err := v.st.GetTranscript(v.ctx, v.st.DB, 1, 1, id)
	if err != nil || journal == nil {
		t.Fatalf("read journal: %v", err)
	}
	var state struct {
		Events []itsaplan.Event `json:"events"`
	}
	if err := json.Unmarshal(journal.State, &state); err != nil {
		t.Fatal(err)
	}
	assertTranscript(t, state.Events, []string{"before\x00after"}, false)
	v.e.Release()
	v.e = v.engine("restarted")
	artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("a", "before\x00after\x00final"))
	v.tick(v.e)
	fr := v.its.Run(1)
	assertTranscript(t, fr.events[1], []string{"before\uFFFDafter\uFFFDfinal"}, true)
	if len(fr.results) != 1 || fr.results[0].Output != "before\uFFFDafter\uFFFDfinal" {
		t.Fatalf("NUL text did not complete through the result outbox: %+v", fr.results)
	}
}

func TestNULDeliveryPreservesEventBoundaries(t *testing.T) {
	v := newArtifactEnv(t)
	v.its.add(1, "coder", 101, "HL-1", "Do it.")
	v.tick(v.e)
	id := v.kg.lastTask()
	artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("a", strings.Repeat("\x00", 12001)))
	v.tick(v.e)
	fr := v.its.Run(1)
	assertTranscript(t, fr.events[1], []string{strings.Repeat("\uFFFD", 12001)}, true)
	if len(fr.events[1]) != 4 || len([]rune(fr.events[1][1].Delta)) != 12000 {
		t.Fatal("NUL replacement changed event counts or exceeded the character limit")
	}
	if len(fr.results) != 1 || fr.results[0].Output != strings.Repeat("\uFFFD", 3333) {
		t.Fatal("NUL replacement broke the final output's UTF-8 byte limit")
	}
}

// replayLegacyHistory follows the base connector's history-only postEvents
// algorithm. Its offset arithmetic intentionally knows nothing of journals.
func replayLegacyHistory(t *testing.T, v *env, task *kagent.Task) {
	t.Helper()
	r := v.run(1)
	events := dispatch.Events(task)
	offset, base := r.EventOffset, r.EventTaskBase
	if r.EventAttempt != r.Attempt {
		offset, base = 0, 0
	}
	if r.EventTaskID != task.ID {
		base = offset
	}
	for {
		start := offset - base
		if start < 0 || start >= len(events) {
			break
		}
		batch := events[start:min(start+200, len(events))]
		next, err := v.its.Events(v.ctx, r.AgentKey, r.ID, r.Attempt, offset, batch)
		if errors.Is(err, itsaplan.ErrConflict) {
			next, err = v.its.Events(v.ctx, r.AgentKey, r.ID, r.Attempt, offset, batch[:1])
		}
		if err != nil || next <= offset {
			t.Fatalf("legacy events: next=%d offset=%d error=%v", next, offset, err)
		}
		offset = next
		if err := v.st.SetEventOffset(v.ctx, r.ID, r.Attempt, offset, task.ID, base); err != nil {
			t.Fatal(err)
		}
	}
}

func TestCompatibilityReleaseCanRollBackToHistoryOnlyConnector(t *testing.T) {
	for _, lostHistoryAck := range []bool{false, true} {
		t.Run(fmt.Sprintf("lostHistoryAck=%t", lostHistoryAck), func(t *testing.T) {
			v := newEnv(t) // production defaults leave artifact admission disabled
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			id := v.kg.lastTask()
			var want []string
			if lostHistoryAck {
				v.kg.say(id, "before rollback")
				want = append(want, "before rollback")
				v.its.loseEventAck = true
			}
			artifactSnapshot(v, id, kagent.StateWorking, textArtifact("a", "native partial"))
			v.tick(v.e)
			if v.run(1).EventOffset != 0 {
				t.Fatal("compatibility release advanced the legacy offset")
			}
			v.e.Release()
			v.kg.say(id, "during rollback")
			task, err := v.kg.GetTask(v.ctx, kagent.AgentRef{Namespace: "factory-homelab", Name: "coder"}, id)
			if err != nil {
				t.Fatal(err)
			}
			replayLegacyHistory(t, v, task)
			want = append(want, "during rollback")
			assertTranscript(t, v.its.Run(1).events[1], want, true)
			v.e = v.engine("reupgraded")
			v.kg.say(id, "after upgrade")
			artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("a", "native final"))
			v.tick(v.e)
			want = append(want, "after upgrade")
			assertTranscript(t, v.its.Run(1).events[1], want, true)
		})
	}
}

func TestCompatibilityReleaseResumesAdmittedArtifactsAfterRollback(t *testing.T) {
	for _, lostAck := range []bool{false, true} {
		t.Run(fmt.Sprintf("lostAck=%t", lostAck), func(t *testing.T) {
			v := newArtifactEnv(t) // models the later activating release
			v.its.add(1, "coder", 101, "HL-1", "Do it.")
			v.tick(v.e)
			id := v.kg.lastTask()
			artifactSnapshot(v, id, kagent.StateWorking, textArtifact("a", "partial"))
			v.its.loseEventAck = lostAck
			v.tick(v.e)
			v.e.Release()
			v.cfg.ArtifactTranscripts = false // lower the pin to this release
			v.e = v.engine("rollback")
			v.kg.say(id, "later history")
			artifactSnapshot(v, id, kagent.StateCompleted, textArtifact("a", "partial then final"))
			v.tick(v.e)
			assertTranscript(t, v.its.Run(1).events[1], []string{"partial then final", "later history"}, true)
			if len(v.its.Run(1).results) != 1 {
				t.Fatal("rollback did not complete the admitted task")
			}
			v.its.add(2, "coder", 102, "HL-2", "Next task.")
			v.tick(v.e)
			artifactSnapshot(v, v.kg.lastTask(), kagent.StateCompleted, textArtifact("a", "new task"))
			v.tick(v.e)
			if len(v.its.Run(2).events[1]) != 0 || len(v.its.Run(2).results) != 1 {
				t.Fatal("rollback admitted a new artifact transcript or lost its result")
			}
		})
	}
}
