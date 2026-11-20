package itsaplan

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNeedsYouUsesCoordinatorAndFiltersJoeWithoutDelegate(t *testing.T) {
	var reads int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads++
		if r.Method != "GET" || r.Header.Get("x-api-key") != "coordinator-key" {
			t.Errorf("unexpected method or identity")
		}
		switch r.URL.Path {
		case "/projects/HOME":
			_, _ = w.Write([]byte(`{"columns":[{"id":1,"stateType":"unstarted"},{"id":2,"stateType":"completed"},{"id":3,"stateType":"canceled"},{"id":4,"stateType":"backlog"},{"id":5,"stateType":"started"}],"assignees":[{"userId":"joe-id","username":"joe","kind":"member"},{"userId":"bot-id","username":"joe","kind":"agent"}]}`))
		case "/projects/HOME/issues":
			q := r.URL.Query()
			if q.Get("assigneeUserId") != "joe-id" || q.Get("limit") != "500" || q.Get("includeArchived") != "false" || q.Has("delegateUserId") {
				t.Errorf("query = %v", q)
			}
			_, _ = w.Write([]byte(`[
				{"id":20,"identifier":"HOME-20","title":"Decision","priority":"high","createdAt":"2026-11-14T12:00:00Z","columnId":1,"assigneeUserId":"joe-id","delegateUserId":null,"description":"must not survive"},
				{"id":21,"columnId":1,"assigneeUserId":"joe-id","delegateUserId":"coder"},
				{"id":22,"columnId":1,"assigneeUserId":"other"},
				{"id":23,"columnId":2,"assigneeUserId":"joe-id"},
				{"id":24,"columnId":3,"assigneeUserId":"joe-id"},
				{"id":25,"columnId":1,"assigneeUserId":"joe-id","archived":true},
				{"id":26,"columnId":99,"assigneeUserId":"joe-id"},
				{"id":27,"columnId":1,"assigneeUserId":null},
				{"id":28,"identifier":"HOME-28","columnId":4,"assigneeUserId":"joe-id","delegateUserId":null},
				{"id":29,"identifier":"HOME-29","columnId":5,"assigneeUserId":"joe-id","delegateUserId":null}
			]`))
		case "/issues/20", "/issues/28", "/issues/29":
			n := strings.TrimPrefix(r.URL.Path, "/issues/")
			fmt.Fprintf(w, `{"identifier":"HOME-%s","columnId":1,"assigneeUserId":"joe-id","description":"## Choices\n1. Ship (Recommended)\n2. Wait"}`, n)
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
		}
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "coordinator-key"}}
	issues, err := c.NeedsYou(context.Background(), "HOME", "joe")
	if err != nil || len(issues) != 3 || reads != 5 {
		t.Fatalf("issues = %+v, reads = %d, err = %v", issues, reads, err)
	}
	if len(issues[0].Choices) != 2 || issues[0].Choices[0] != "Ship" || issues[0].ID != 20 || issues[1].ID != 28 || issues[2].ID != 29 {
		t.Fatalf("wrong issue filter: %+v", issues)
	}
	data, _ := json.Marshal(issues[0])
	var fields map[string]any
	_ = json.Unmarshal(data, &fields)
	if _, ok := fields["description"]; ok {
		t.Fatal("description retained")
	}
}

func TestNeedsYouRejectsMissingMemberAndFullListing(t *testing.T) {
	for _, full := range []bool{false, true} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/projects/HOME" {
				if !full {
					_, _ = w.Write([]byte(`{"assignees":[]}`))
					return
				}
				_, _ = w.Write([]byte(`{"assignees":[{"userId":"joe-id","username":"joe","kind":"member"}]}`))
				return
			}
			_ = json.NewEncoder(w).Encode(make([]IssueSummary, 500))
		}))
		c := &Client{BaseURL: srv.URL, Coordinator: "coord", Keys: map[string]string{"coord": "key"}}
		_, err := c.NeedsYou(context.Background(), "HOME", "joe")
		srv.Close()
		if err == nil || errors.Is(err, ErrNotMember) == full {
			t.Fatalf("full=%v: err = %v", full, err)
		}
	}
}

func TestChoiceCounts(t *testing.T) {
	desc := "## Choices\n1. First (Recommended)"
	for i := 1; i <= 5; i++ {
		if i > 1 {
			desc += fmt.Sprintf("\n%d. Next", i)
		}
		want := i
		if i == 1 || i == 5 {
			want = 0
		}
		if got := len(parseChoices(desc)); got != want {
			t.Errorf("%d items: got %d", i, got)
		}
	}
	if parseChoices("## Choices\n1. First\n2. Second") != nil {
		t.Fatal("missing recommendation accepted")
	}
}

func TestChoicesIgnoreCodeAndHTMLBlocks(t *testing.T) {
	for _, valid := range []string{
		"## Choices\n1. Ship (Recommended)\n2. Hold",
		"<!-- note -->\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"<!--\n## Choices\n1. Old (Recommended)\n2. Older\n-->\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"<pre>\nexample\n</pre>\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"<div>note</div>\n\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"See:\n<https://example.com>\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"<https://example.com>\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"Text\n<span class=\"x\">\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"```\n    ```\n## Choices\n1. Old (Recommended)\n2. Older\n```\n## Choices\n1. Ship (Recommended)\n2. Hold",
	} {
		if len(parseChoices(valid)) != 2 {
			t.Fatalf("valid section rejected: %q", valid)
		}
	}
	for _, desc := range []string{
		"Example:\n\n    ## Choices\n    1. Ship (Recommended)\n    2. Hold",
		"Example:\n\n\t## Choices\n\t1. Ship (Recommended)\n\t2. Hold",
		"## Choices\n1. Ship (Recommended)\n2. Hold\n\n    3. Code",
		"Example:\n```\n## Choices\n1. Ship (Recommended)\n2. Hold\n```",
		"<!--\n## Choices\n1. Ship (Recommended)\n2. Hold\n-->",
		"<!-- draft\n## Choices\n1. Ship (Recommended)\n2. Hold -->",
		"## Choices\n1. Ship (Recommended)\n<!-- 2. Hold -->",
		"<pre>\n## Choices\n1. Ship (Recommended)\n2. Hold\n</pre>",
		"<PRE class=x>\n## Choices\n1. Ship (Recommended)\n2. Hold\n</PRE>",
		"<script>\n## Choices\n1. Ship (Recommended)\n2. Hold\n</script>",
		"<details>\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"<?x\n## Choices\n1. Ship (Recommended)\n2. Hold\n?>",
		"## Choices\n1. Ship (Recommended)\n<div>2. Hold</div>",
		"Example:\n```\n    ```\n## Choices\n1. Ship (Recommended)\n2. Hold\n```",
		"<span class=\"x\">\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"</section>\n## Choices\n1. Ship (Recommended)\n2. Hold",
		"## Choices\n1. Ship (Recommended)\n\n   2. Wait",
		"## Choices\n1. Ship (Recommended)\n 2. Wait",
	} {
		if got := parseChoices(desc); got != nil {
			t.Errorf("code block parsed as choices: %q", got)
		}
	}
}
