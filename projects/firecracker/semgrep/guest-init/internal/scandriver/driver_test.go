package scandriver

import (
	"bufio"
	"encoding/json"
	"io"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func testDriver(t *testing.T, requests int) (*Driver, <-chan error) {
	t.Helper()
	requestReader, requestWriter := io.Pipe()
	responseReader, responseWriter := io.Pipe()
	driver := &Driver{stdin: requestWriter, stdout: bufio.NewReader(responseReader)}
	done := make(chan error, 1)

	go func() {
		defer requestReader.Close()
		defer responseWriter.Close()
		decoder := json.NewDecoder(requestReader)
		encoder := json.NewEncoder(responseWriter)
		for range requests {
			var wire struct {
				Files []struct {
					File string `json:"file"`
				} `json:"files"`
			}
			if err := decoder.Decode(&wire); err != nil {
				done <- err
				return
			}
			path := ""
			if len(wire.Files) > 0 {
				path = wire.Files[0].File
			}
			response := map[string]any{
				"results": []map[string]any{{
					"check_id": "rule.id",
					"path":     path,
					"start":    map[string]int{"line": 1, "col": 1},
					"extra":    map[string]string{"message": "m", "severity": "WARNING"},
				}},
				"errors": []any{},
			}
			if err := encoder.Encode(response); err != nil {
				done <- err
				return
			}
		}
		done <- nil
	}()

	return driver, done
}

func TestScanSuccessiveMetadataDoesNotLeak(t *testing.T) {
	driver, done := testDriver(t, 3)
	requests := []vsockproto.ScanRequest{
		{CorrelationID: "first-id", Files: []vsockproto.ScanFile{{Path: "first.py"}}},
		{CorrelationID: "second-id", Files: []vsockproto.ScanFile{{Path: "second.py"}}},
		{Files: []vsockproto.ScanFile{{Path: "untagged.py"}}},
	}

	for _, req := range requests {
		result, err := driver.Scan(req)
		if err != nil {
			t.Fatalf("Scan(%q): %v", req.CorrelationID, err)
		}
		if result.CorrelationID != req.CorrelationID {
			t.Errorf("Scan(%q) correlation_id = %q", req.CorrelationID, result.CorrelationID)
		}
		if len(result.Findings) != 1 || result.Findings[0].Path != req.Files[0].Path {
			t.Errorf("Scan(%q) findings = %+v", req.CorrelationID, result.Findings)
		}
	}
	if err := <-done; err != nil {
		t.Fatalf("fake scan-server: %v", err)
	}
}

func TestScanOverlapsAreSerializedWithoutMetadataLeakage(t *testing.T) {
	driver, done := testDriver(t, 2)
	requests := []vsockproto.ScanRequest{
		{CorrelationID: "overlap-one", Files: []vsockproto.ScanFile{{Path: "one.py"}}},
		{CorrelationID: "overlap-two", Files: []vsockproto.ScanFile{{Path: "two.py"}}},
	}

	type outcome struct {
		request vsockproto.ScanRequest
		result  vsockproto.ScanResult
		err     error
	}
	outcomes := make(chan outcome, len(requests))
	var wg sync.WaitGroup
	for _, req := range requests {
		req := req
		wg.Add(1)
		go func() {
			defer wg.Done()
			result, err := driver.Scan(req)
			outcomes <- outcome{request: req, result: result, err: err}
		}()
	}
	wg.Wait()
	close(outcomes)

	for out := range outcomes {
		if out.err != nil {
			t.Fatalf("Scan(%q): %v", out.request.CorrelationID, out.err)
		}
		if out.result.CorrelationID != out.request.CorrelationID {
			t.Errorf("Scan(%q) correlation_id = %q", out.request.CorrelationID, out.result.CorrelationID)
		}
		if len(out.result.Findings) != 1 || out.result.Findings[0].Path != out.request.Files[0].Path {
			t.Errorf("Scan(%q) findings = %+v", out.request.CorrelationID, out.result.Findings)
		}
	}
	if err := <-done; err != nil {
		t.Fatalf("fake scan-server: %v", err)
	}
}

func TestScanErrorStillReturnsCorrelationID(t *testing.T) {
	driver := &Driver{
		stdin:  failingWriter{},
		stdout: bufio.NewReader(nil),
	}
	result, err := driver.Scan(vsockproto.ScanRequest{CorrelationID: "failed-id"})
	if err == nil {
		t.Fatal("Scan returned nil error")
	}
	if result.CorrelationID != "failed-id" {
		t.Fatalf("correlation_id = %q, want failed-id", result.CorrelationID)
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }
func (failingWriter) Close() error              { return nil }
