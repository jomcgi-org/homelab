package driver

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"sync"
	"testing"
)

func TestTagWriterPartialAndMultilineWrites(t *testing.T) {
	var output bytes.Buffer
	w := newTagWriter(slog.New(slog.NewJSONHandler(&output, nil)), "scratch-postgres", "init")

	if n, err := w.Write([]byte("boot par")); err != nil || n != len("boot par") {
		t.Fatalf("first Write = (%d, %v), want (%d, nil)", n, err, len("boot par"))
	}
	if output.Len() != 0 {
		t.Fatalf("partial Write emitted output %q", output.String())
	}
	input := []byte("tial\nready\n\ntrailing")
	if n, err := w.Write(input); err != nil || n != len(input) {
		t.Fatalf("second Write = (%d, %v), want (%d, nil)", n, err, len(input))
	}

	records := decodeLogRecords(t, output.Bytes())
	if len(records) != 3 {
		t.Fatalf("completed records = %d, want 3: %q", len(records), output.String())
	}
	wantMessages := []string{"boot partial", "ready", ""}
	for i, record := range records {
		assertGuestRecord(t, record, wantMessages[i], "scratch-postgres", "init")
	}

	w.Flush()
	records = decodeLogRecords(t, output.Bytes())
	if len(records) != 4 {
		t.Fatalf("records after Flush = %d, want 4: %q", len(records), output.String())
	}
	assertGuestRecord(t, records[3], "trailing", "scratch-postgres", "init")
	w.Flush()
	if got := len(decodeLogRecords(t, output.Bytes())); got != 4 {
		t.Fatalf("second Flush emitted another record, got %d records", got)
	}
}

func TestTagWriterPreservesGuestJSONAsOpaqueMessage(t *testing.T) {
	var output bytes.Buffer
	w := newTagWriter(slog.New(slog.NewJSONHandler(&output, nil)), "api", "vm")
	guestJSON := `{"level":"error","source":"inside","answer":42}`
	if _, err := w.Write([]byte(guestJSON + "\n")); err != nil {
		t.Fatalf("Write: %v", err)
	}

	records := decodeLogRecords(t, output.Bytes())
	if len(records) != 1 {
		t.Fatalf("records = %d, want 1", len(records))
	}
	assertGuestRecord(t, records[0], guestJSON, "api", "vm")
	if _, promoted := records[0]["answer"]; promoted {
		t.Fatalf("guest JSON key was promoted into outer record: %#v", records[0])
	}
	if got := records[0]["level"]; got != "INFO" {
		t.Fatalf("outer level = %#v, want INFO", got)
	}
}

func TestTagWriterConcurrentWritesKeepCompleteLines(t *testing.T) {
	var output lockedBuffer
	w := newTagWriter(slog.New(slog.NewJSONHandler(&output, nil)), "muxed", "vm")
	const writers = 16
	var wg sync.WaitGroup
	for range writers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if n, err := w.Write([]byte("complete\n")); err != nil || n != len("complete\n") {
				t.Errorf("Write = (%d, %v), want (%d, nil)", n, err, len("complete\n"))
			}
		}()
	}
	wg.Wait()

	records := decodeLogRecords(t, output.Bytes())
	if len(records) != writers {
		t.Fatalf("records = %d, want %d", len(records), writers)
	}
	for _, record := range records {
		assertGuestRecord(t, record, "complete", "muxed", "vm")
	}
}

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.Write(p)
}

func (b *lockedBuffer) Bytes() []byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return bytes.Clone(b.b.Bytes())
}

func decodeLogRecords(t *testing.T, output []byte) []map[string]any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(output))
	var records []map[string]any
	for dec.More() {
		var record map[string]any
		if err := dec.Decode(&record); err != nil {
			t.Fatalf("decode slog record: %v\noutput: %s", err, output)
		}
		records = append(records, record)
	}
	return records
}

func assertGuestRecord(t *testing.T, record map[string]any, message, workload, phase string) {
	t.Helper()
	want := map[string]string{
		"msg":      message,
		"source":   "guest",
		"workload": workload,
		"phase":    phase,
	}
	for key, value := range want {
		if got := record[key]; got != value {
			t.Errorf("record[%q] = %#v, want %q; record: %#v", key, got, value, record)
		}
	}
}
