package discord

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Renderer turns a diagram, chart or SVG source into a PNG
// (factory-discord-render).
type Renderer interface {
	Render(ctx context.Context, kind string, spec []byte) ([]byte, error)
}

// Bounds on a reply's images.
const (
	maxImages     = 4
	maxImageBytes = 8 << 20
	renderTimeout = 15 * time.Second
)

// visualBlock is a fenced code block that the renderer draws. The language
// tag names the renderer route.
var visualBlock = regexp.MustCompile("(?s)```[ \\t]*(mermaid|vega-lite|vegalite|svg)[ \\t]*\\r?\\n(.*?)```")

// routes maps a block's language tag to the renderer's route.
var routes = map[string]string{"mermaid": "mermaid", "vega-lite": "vega-lite", "vegalite": "vega-lite", "svg": "svg"}

// imageMark holds a rendered image's place in Visuals' text. Visuals removes
// it from the answer first, so each one in its text is an image.
const imageMark = "\x00"

// Visuals renders the first maxImages diagram, chart and SVG blocks in an
// answer. A rendered block leaves the text and becomes a PNG; imageMark keeps
// its place, so WithImages shows it where the agent drew it. A block the
// renderer refuses stays as text. memo is the text to store in memory: the
// answer with each rendered block named, not its source.
func Visuals(ctx context.Context, r Renderer, answer string) (text, memo string, files []File) {
	answer = strings.ReplaceAll(answer, imageMark, "")
	if r == nil {
		return answer, answer, nil
	}
	var out, mem strings.Builder
	last := 0
	for _, m := range visualBlock.FindAllStringSubmatchIndex(answer, -1) {
		if len(files) == maxImages {
			break
		}
		kind := routes[answer[m[2]:m[3]]]
		spec := answer[m[4]:m[5]]
		rctx, cancel := context.WithTimeout(ctx, renderTimeout)
		png, err := r.Render(rctx, kind, []byte(spec))
		cancel()
		if err != nil || len(png) == 0 || len(png) > maxImageBytes {
			continue
		}
		out.WriteString(answer[last:m[0]])
		out.WriteString(imageMark)
		mem.WriteString(answer[last:m[0]])
		fmt.Fprintf(&mem, "[image %d: %s]", len(files)+1, kind)
		files = append(files, File{Name: fmt.Sprintf("image-%d.png", len(files)+1), Data: png})
		last = m[1]
	}
	out.WriteString(answer[last:])
	mem.WriteString(answer[last:])
	return strings.TrimSpace(out.String()), strings.TrimSpace(mem.String()), files
}

// WithImages places files in a reply at their marks in text: each run of
// text becomes a text display and each run of images one media gallery, in
// answer order. Text past Discord's limit is cut; the images stay.
func WithImages(r Reply, text string, files []File) Reply {
	if len(files) == 0 {
		return r
	}
	r.Components = nil
	budget := maxReplyChars
	var gallery *ReplyComponent
	for n, part := range strings.Split(text, imageMark) {
		if part = strings.TrimSpace(part); part != "" && budget > 0 {
			if utf8.RuneCountInString(part) > budget {
				part = string([]rune(part)[:budget-1]) + "…"
			}
			budget -= utf8.RuneCountInString(part)
			r.Components = append(r.Components, ReplyComponent{Type: componentText, Content: part})
			gallery = nil
		}
		if n >= len(files) {
			continue
		}
		if gallery == nil {
			r.Components = append(r.Components, ReplyComponent{Type: componentGallery})
			gallery = &r.Components[len(r.Components)-1]
		}
		var item MediaItem
		item.Media.URL = "attachment://" + files[n].Name
		gallery.Items = append(gallery.Items, item)
		r.Attachments = append(r.Attachments, ReplyAttachment{ID: n, Filename: files[n].Name})
	}
	r.Files = files
	return r
}

// HTTPRenderer calls factory-discord-render.
type HTTPRenderer struct {
	BaseURL string
	HTTP    *http.Client
}

// ErrRenderRefused is a renderer answer other than a PNG.
var ErrRenderRefused = errors.New("discord: renderer refused the spec")

// Render posts spec to the kind's route and returns the PNG.
func (h *HTTPRenderer) Render(ctx context.Context, kind string, spec []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimSuffix(h.BaseURL, "/")+"/"+kind, bytes.NewReader(spec))
	if err != nil {
		return nil, err
	}
	client := h.HTTP
	if client == nil {
		client = &http.Client{Timeout: renderTimeout}
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxImageBytes+1))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK || resp.Header.Get("Content-Type") != "image/png" {
		return nil, fmt.Errorf("%w: HTTP %d", ErrRenderRefused, resp.StatusCode)
	}
	return data, nil
}
