package discord

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeRenderer struct {
	kinds []string
}

func (f *fakeRenderer) Render(_ context.Context, kind string, spec []byte) ([]byte, error) {
	f.kinds = append(f.kinds, kind)
	if strings.Contains(string(spec), "bad") {
		return nil, ErrRenderRefused
	}
	return []byte("png:" + kind), nil
}

func TestVisualsRendersBlocksAndKeepsRefusedOnesAsText(t *testing.T) {
	r := &fakeRenderer{}
	answer := "Here:\n```mermaid\nflowchart LR\nA-->B\n```\nand\n```svg\n<svg/>\n```\n" +
		"```vega-lite\n{\"bad\":1}\n```\nend"
	text, memo, files := Visuals(context.Background(), r, answer)
	if len(files) != 2 || string(files[0].Data) != "png:mermaid" || files[1].Name != "image-2.png" {
		t.Fatalf("files = %+v", files)
	}
	if strings.Contains(text, "flowchart") || strings.Contains(text, "<svg") || !strings.Contains(text, "{\"bad\":1}") {
		t.Errorf("text = %q", text)
	}
	if !strings.Contains(memo, "[image 1: mermaid]") || !strings.Contains(memo, "[image 2: svg]") || strings.Contains(memo, "flowchart") {
		t.Errorf("memo = %q", memo)
	}
	if strings.Join(r.kinds, ",") != "mermaid,svg,vega-lite" {
		t.Errorf("routes = %v", r.kinds)
	}
}

func TestVisualsStopsAtTheImageLimitAndWithoutARenderer(t *testing.T) {
	answer := strings.Repeat("```svg\n<svg/>\n```\n", maxImages+1)
	_, _, files := Visuals(context.Background(), &fakeRenderer{}, answer)
	if len(files) != maxImages {
		t.Errorf("rendered %d images, want %d", len(files), maxImages)
	}
	if text, memo, files := Visuals(context.Background(), nil, answer); text != answer || memo != answer || files != nil {
		t.Error("a nil renderer changed the answer")
	}
}

func TestWithImagesPlacesImagesWhereTheyWereDrawn(t *testing.T) {
	r := &fakeRenderer{}
	answer := "Polished:\n```svg\n<svg/>\n```\n\nWant a storm version?\n```svg\n<svg/>\n```\n```svg\n<svg/>\n```"
	text, _, files := Visuals(context.Background(), r, answer)
	got := WithImages(NewReply(text, "9"), text, files).Components
	if len(got) != 4 || got[0].Content != "Polished:" || len(got[1].Items) != 1 ||
		got[2].Content != "Want a storm version?" || len(got[3].Items) != 2 ||
		got[3].Items[1].Media.URL != "attachment://image-3.png" {
		t.Errorf("components = %+v", got)
	}
}

func TestVisualsDropsMarksTheAgentWrote(t *testing.T) {
	text, _, files := Visuals(context.Background(), &fakeRenderer{}, "a"+imageMark+"b")
	if text != "ab" || files != nil {
		t.Errorf("text %q, files %v", text, files)
	}
}

func TestWithImagesKeepsImagesWhenTextIsCut(t *testing.T) {
	text := strings.Repeat("x", maxReplyChars+10) + imageMark + "after"
	got := WithImages(NewReply(text, "9"), text, []File{{Name: "image-1.png"}}).Components
	if len(got) != 2 || len([]rune(got[0].Content)) != maxReplyChars || len(got[1].Items) != 1 {
		t.Errorf("components = %d", len(got))
	}
}

func TestWithImagesAddsOneGalleryAndDropsEmptyText(t *testing.T) {
	files := []File{{Name: "image-1.png", Data: []byte("a")}, {Name: "image-2.png", Data: []byte("b")}}
	r := WithImages(NewReply("", "9"), imageMark+imageMark, files)
	raw, _ := json.Marshal(r)
	want := `"components":[{"type":12,"items":[{"media":{"url":"attachment://image-1.png"}},{"media":{"url":"attachment://image-2.png"}}]}]`
	if !strings.Contains(string(raw), want) || !strings.Contains(string(raw), `"attachments":[{"id":0,"filename":"image-1.png"},{"id":1,"filename":"image-2.png"}]`) {
		t.Errorf("reply = %s", raw)
	}
	if strings.Contains(string(raw), "no answer") || strings.Contains(string(raw), "Data") {
		t.Errorf("reply = %s", raw)
	}
	if r := WithImages(NewReply("hi", "9"), "hi"+imageMark+imageMark, files); len(r.Components) != 2 || r.Components[0].Content != "hi" {
		t.Errorf("components = %+v", r.Components)
	}
}

func TestSendUploadsFilesAsMultipart(t *testing.T) {
	var payload string
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if err := req.ParseMultipartForm(1 << 20); err != nil {
			t.Errorf("not multipart: %v", err)
			return
		}
		payload = req.FormValue("payload_json")
		for n := range 2 {
			f, h, err := req.FormFile("files[" + string(rune('0'+n)) + "]")
			if err != nil {
				t.Errorf("files[%d]: %v", n, err)
				return
			}
			data, _ := io.ReadAll(f)
			got = append(got, h.Filename+"="+string(data))
		}
	}))
	defer srv.Close()
	rest := &REST{Base: srv.URL, Token: func() (string, error) { return "t", nil }}
	files := []File{{Name: "image-1.png", Data: []byte("a")}, {Name: "image-2.png", Data: []byte("b")}}
	if err := rest.Send(context.Background(), "5", WithImages(NewReply("x", "9"), "x"+imageMark+imageMark, files)); err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, ",") != "image-1.png=a,image-2.png=b" || !strings.Contains(payload, `"allowed_mentions":{"parse":[]`) {
		t.Errorf("files %v, payload %s", got, payload)
	}
}

func TestHTTPRendererAcceptsOnlyAPNG(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/svg" {
			w.Header().Set("Content-Type", "image/png")
			_, _ = w.Write([]byte("png"))
			return
		}
		http.Error(w, "Unsupported or external input", http.StatusUnprocessableEntity)
	}))
	defer srv.Close()
	h := &HTTPRenderer{BaseURL: srv.URL}
	if png, err := h.Render(context.Background(), "svg", []byte("<svg/>")); err != nil || string(png) != "png" {
		t.Errorf("svg = %q, %v", png, err)
	}
	if _, err := h.Render(context.Background(), "mermaid", []byte("x")); !errors.Is(err, ErrRenderRefused) {
		t.Errorf("refused render error = %v", err)
	}
}

func TestATurnRepliesWithImagesAndStoresTheirNames(t *testing.T) {
	agent := &fakeAgent{answer: "A pelican:\n```svg\n<svg/>\n```"}
	b, mem, rep := newBridge(agent)
	b.Render = &fakeRenderer{}
	b.Turn(context.Background(), tagged("1", "draw a pelican"))
	if len(rep.sent) != 1 || len(rep.sent[0].Files) != 1 || rep.sent[0].Components[0].Content != "A pelican:" {
		t.Fatalf("sent = %+v", rep.sent)
	}
	if len(mem.retained) != 1 || !strings.Contains(mem.retained[0], "A pelican:\n[image 1: svg]") || strings.Contains(mem.retained[0], "<svg") {
		t.Errorf("retained = %v", mem.retained)
	}
}
