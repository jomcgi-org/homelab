package kagent

import (
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
)

type repeatedByte byte

func (b repeatedByte) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = byte(b)
	}
	return len(p), nil
}

func TestMetadataDiscardsOversizedStatusBeforeApplyingBudget(t *testing.T) {
	// The source generates the large value without allocating it. Metadata
	// includes a following task and pagination fields after the ignored text.
	body := io.MultiReader(
		strings.NewReader(`{"result":{"tasks":[{"id":"older","contextId":"c","status":{"state":"TASK_STATE_COMPLETED","message":{"parts":[{"text":"`),
		io.LimitReader(repeatedByte('x'), maxResponseBytes+1),
		strings.NewReader(`"}]}}},{"id":"target","contextId":"c","status":{"state":"TASK_STATE_WORKING"}}],"pageSize":100,"totalSize":2,"nextPageToken":"next"}}`),
	)
	data, err := taskMetadataJSON(body)
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Result TaskPage `json:"result"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		t.Fatal(err)
	}
	page := envelope.Result
	if len(data) > 512 || len(page.Tasks) != 2 || page.Tasks[0].Status.Message != nil || page.Tasks[1].ID != "target" || page.Tasks[1].Status.State != StateWorking || page.NextPageToken != "next" || page.TotalSize != 2 || page.PageSize != 100 {
		t.Fatalf("metadata was lost or retained payload: bytes=%d page=%+v", len(data), page)
	}
}

func TestMetadataRetainedValuesRemainBounded(t *testing.T) {
	body := io.MultiReader(strings.NewReader(`{"result":{"tasks":[{"id":"`), io.LimitReader(repeatedByte('x'), maxResponseBytes+1), strings.NewReader(`"}]}}`))
	if _, err := taskMetadataJSON(body); !errors.Is(err, ErrResponseTooLarge) {
		t.Fatalf("unbounded task identity: %v", err)
	}
	body = io.MultiReader(strings.NewReader(`{"result":{"id":"`), io.LimitReader(repeatedByte('x'), maxResponseBytes+1), strings.NewReader(`"}}`))
	if _, err := exactTaskMetadataJSON(body); !errors.Is(err, ErrResponseTooLarge) {
		t.Fatalf("unbounded exact task identity: %v", err)
	}
}

func TestExactMetadataDiscardsOversizedOutput(t *testing.T) {
	body := io.MultiReader(
		strings.NewReader(`{"result":{"artifacts":[{"parts":[{"text":"`),
		io.LimitReader(repeatedByte('x'), maxResponseBytes+1),
		strings.NewReader(`"}]}],"status":{"message":{"parts":[{"text":"`),
		io.LimitReader(repeatedByte('y'), maxResponseBytes+1),
		strings.NewReader(`"}]},"state":"TASK_STATE_COMPLETED"},"id":"target","contextId":"c"}}`),
	)
	data, err := exactTaskMetadataJSON(body)
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Result Task `json:"result"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		t.Fatal(err)
	}
	task := envelope.Result
	if len(data) > 512 || task.ID != "target" || task.ContextID != "c" || task.Status.State != StateCompleted || (task.Status.Message != nil && task.Status.Message.Parts[0].Text != "") || task.Artifacts != nil {
		t.Fatalf("exact metadata lost identity or retained output: bytes=%d task=%+v", len(data), task)
	}
}

func TestMetadataValidatesDiscardedValues(t *testing.T) {
	for _, ignored := range []string{
		`"truncated`, `"bad\q"`, `"bad\u00xz"`, "\"control\n\"",
		`{"parts": [}`, `{"a": 1,}`, `[1,]`, `01`, `1.`, `1e+`, `truth`,
	} {
		t.Run(ignored, func(t *testing.T) {
			body := `{"result":{"tasks":[{"id":"target","contextId":"c","status":{"state":"TASK_STATE_COMPLETED","message":` + ignored + `}}],"totalSize":1,"pageSize":100,"nextPageToken":""}}`
			if data, err := taskMetadataJSON(strings.NewReader(body)); err == nil || data != nil {
				t.Fatalf("malformed discarded value supplied terminal evidence: %q, %v", data, err)
			}
		})
	}
	for _, body := range []string{
		`{"result":{"tasks":[{"id":"target","status":{"state":"TASK_STATE_COMPLETED"}}]`,
		`{"result":{}} {"result":{}}`,
		strings.Repeat("[", 1001) + strings.Repeat("]", 1001),
	} {
		if _, err := taskMetadataJSON(strings.NewReader(body)); err == nil {
			t.Fatalf("accepted truncated, trailing or excessively nested JSON (bytes=%d)", len(body))
		}
	}
}

func TestMetadataPreservesEscapedKeysAndRPCError(t *testing.T) {
	const body = `{"result":{"tasks":[{"\u0069d":"target","contextId":"c","status":{"state":"TASK_STATE_COMPLETED","message":{"text":"quotes: \" and slashes: \\"}}}],"totalSize":1,"pageSize":100,"nextPageToken":""},"unknown":[null,true,false,-0,1.25e-3,{"nested":[]}],"error":{"code":-32001,"message":"task not found"}}`
	data, err := taskMetadataJSON(strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	var response rpcResponse
	if err := json.Unmarshal(data, &response); err != nil {
		t.Fatal(err)
	}
	var page TaskPage
	if err := json.Unmarshal(response.Result, &page); err != nil {
		t.Fatal(err)
	}
	if page.Tasks[0].ID != "target" || page.Tasks[0].Status.State != StateCompleted || page.Tasks[0].Status.Message != nil || response.Error == nil || response.Error.Code != -32001 {
		t.Fatalf("projection changed evidence: %s", data)
	}
}

func FuzzTaskMetadataJSON(f *testing.F) {
	for _, body := range []string{
		`{"result":{"tasks":[],"totalSize":0,"pageSize":100,"nextPageToken":""}}`,
		`{"result":{"tasks":[{"status":{"state":"TASK_STATE_COMPLETED","message":{"parts":[{"text":"a\"b\\c"}]}}}]}}`,
		`{"ignored":[true,false,null,-1.2e+3,{"text":"\u0041"}]}`,
		`{"error":{"code":-32001,"message":"missing"}}`,
		`null`, `1`, `"text"`, `[]`, `{"ignored":"bad\q"}`,
	} {
		f.Add(body)
	}
	f.Fuzz(func(t *testing.T, body string) {
		// These inputs cannot reach the size, field-name or nesting budgets.
		if len(body) > 1024 {
			return
		}
		for _, project := range []func(io.Reader) ([]byte, error){taskMetadataJSON, exactTaskMetadataJSON} {
			data, err := project(strings.NewReader(body))
			if json.Valid([]byte(body)) {
				if err != nil || !json.Valid(data) {
					t.Fatalf("valid JSON rejected: %q, %v", body, err)
				}
			} else if err == nil {
				t.Fatalf("invalid JSON accepted: %q", body)
			}
		}
	})
}

func TestMetadataNestingBoundary(t *testing.T) {
	for _, project := range []func(io.Reader) ([]byte, error){taskMetadataJSON, exactTaskMetadataJSON} {
		for _, depth := range []int{1000, 1001} {
			body := strings.Repeat("[", depth) + "null" + strings.Repeat("]", depth)
			_, err := project(strings.NewReader(body))
			if (err == nil) != (depth == 1000) {
				t.Fatalf("depth=%d error=%v", depth, err)
			}
		}
	}
}
