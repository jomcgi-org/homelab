package discord

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// capabilityMatrix is a table like the one Bosun posted as raw pipes.
const capabilityMatrix = "What I can do:\n\n" +
	"| Capability | Status | Notes |\n" +
	"|---|:---:|---|\n" +
	"| **Diagrams** | ✅ | Mermaid, `vega-lite` |\n" +
	"| Web search | 🚧 | [soon](https://example.org) |\n" +
	"| Moderation | ❌ | Not planned |\n" +
	"\nAsk away."

type tableRenderer struct {
	fakeRenderer
	specs [][]byte
}

func (f *tableRenderer) Render(ctx context.Context, kind string, spec []byte) ([]byte, error) {
	f.specs = append(f.specs, spec)
	return f.fakeRenderer.Render(ctx, kind, spec)
}

func TestVisualsDrawsATableWhereItWas(t *testing.T) {
	r := &tableRenderer{}
	text, memo, files := Visuals(context.Background(), r, capabilityMatrix)
	if len(files) != 1 || string(files[0].Data) != "png:table" {
		t.Fatalf("files = %+v", files)
	}
	if text != "What I can do:\n\n"+imageMark+"\nAsk away." {
		t.Errorf("text = %q", text)
	}
	// Memory keeps the table as text.
	if !strings.Contains(memo, "| Web search | 🚧 | [soon](https://example.org) |") {
		t.Errorf("memo = %q", memo)
	}
	var spec struct {
		Columns []string   `json:"columns"`
		Align   []string   `json:"align"`
		Rows    [][]string `json:"rows"`
	}
	if err := json.Unmarshal(r.specs[0], &spec); err != nil {
		t.Fatal(err)
	}
	if strings.Join(spec.Columns, ",") != "Capability,Status,Notes" ||
		strings.Join(spec.Align, ",") != "left,center,left" || len(spec.Rows) != 3 ||
		strings.Join(spec.Rows[0], ",") != "Diagrams,✅,Mermaid, vega-lite" ||
		spec.Rows[1][2] != "soon" {
		t.Errorf("spec = %+v", spec)
	}
}

func TestVisualsFallsBackToAMonospaceTable(t *testing.T) {
	table := func(last string) string {
		return "```\n" +
			"Capability | Status | Notes\n" +
			"-----------+--------+-------------------\n" +
			"Diagrams   |   ✅   | Mermaid, vega-lite\n" +
			"Web search |   🚧   | soon\n" +
			"Moderation |   ❌   | " + last + "\n" +
			"```"
	}
	refused := strings.Replace(capabilityMatrix, "Not planned", "bad", 1)
	full := strings.Repeat("```svg\n<svg/>\n```\n", maxImages) + capabilityMatrix
	for _, c := range []struct {
		name   string
		r      Renderer
		answer string
		want   string
		images int
	}{
		{"no renderer", nil, capabilityMatrix, table("Not planned"), 0},
		{"refused", &fakeRenderer{}, refused, table("bad"), 0},
		{"past the image limit", &fakeRenderer{}, full, table("Not planned"), maxImages},
	} {
		text, memo, files := Visuals(context.Background(), c.r, c.answer)
		if !strings.Contains(text, c.want) || strings.Contains(text, "|---|") {
			t.Errorf("%s: text = %q", c.name, text)
		}
		if !strings.Contains(memo, "|---|:---:|---|") {
			t.Errorf("%s: memo = %q", c.name, memo)
		}
		if len(files) != c.images {
			t.Errorf("%s: %d images, want %d", c.name, len(files), c.images)
		}
	}
}

func TestFindTablesIgnoresCodeAndProse(t *testing.T) {
	answer := "a | b is not a table\n\n" +
		"```\n| x | y |\n|---|---|\n| 1 | 2 |\n```\n" +
		"| one |\n|---|---|\n" + // the delimiter has more cells than the header
		"| p | q \\| r |\n| - | -: |\n| 1 | 2 | extra |\n| 3 |\n"
	tables := findTables(answer)
	if len(tables) != 1 {
		t.Fatalf("tables = %+v", tables)
	}
	got := tables[0]
	if strings.Join(got.columns, ",") != "p,q | r" || strings.Join(got.align, ",") != "left,right" ||
		len(got.rows) != 2 || strings.Join(got.rows[0], ",") != "1,2" || strings.Join(got.rows[1], ",") != "3," {
		t.Errorf("table = %+v", got)
	}
	if answer[got.end-1] != '\n' || !strings.HasPrefix(answer[got.start:], "| p |") {
		t.Errorf("span %d..%d", got.start, got.end)
	}
}

func TestFindTablesFollowsFenceLength(t *testing.T) {
	answer := "````markdown\n```\n| x | y |\n|---|---|\n````\n| a | b |\n|---|---|\n| 1 | 2 |\n"
	tables := findTables(answer)
	if len(tables) != 1 || tables[0].columns[0] != "a" {
		t.Errorf("tables = %+v", tables)
	}
}

type downRenderer struct{ calls int }

func (d *downRenderer) Render(context.Context, string, []byte) ([]byte, error) {
	d.calls++
	return nil, context.DeadlineExceeded
}

func TestVisualsStopsCallingADownRenderer(t *testing.T) {
	d := &downRenderer{}
	text, _, files := Visuals(context.Background(), d, capabilityMatrix+"\n\n"+capabilityMatrix)
	if d.calls != 1 || len(files) != 0 || strings.Count(text, "Capability | Status | Notes") != 2 {
		t.Errorf("calls %d, files %d, text %q", d.calls, len(files), text)
	}
}

func TestDisplayWidthCountsEmojiAsTwo(t *testing.T) {
	for s, want := range map[string]int{"abc": 3, "✅": 2, "🚧": 2, "❤️": 2, "👩‍💻": 4, "日本": 4} {
		if got := displayWidth(s); got != want {
			t.Errorf("displayWidth(%q) = %d, want %d", s, got, want)
		}
	}
}
