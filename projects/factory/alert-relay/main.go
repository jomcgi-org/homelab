package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode/utf16"
)

type alert struct {
	Status              string
	Labels, Annotations map[string]string
	StartsAt, EndsAt    time.Time
}

type embed struct {
	Title       string            `json:"title"`
	URL         string            `json:"url,omitempty"`
	Description string            `json:"description,omitempty"`
	Color       int               `json:"color"`
	Image       map[string]string `json:"image,omitempty"`
}

// clip fits text into a Discord field without splitting a surrogate pair.
func clip(text string, budget int) string {
	for i, r := range text {
		budget -= utf16.RuneLen(r)
		if budget < 0 {
			return text[:i]
		}
	}
	return text
}

// exploreURL uses the datasource UID provisioned in platform-factory/grafana.
func exploreURL(expr string) string {
	if expr == "" {
		return ""
	}
	pane := map[string]any{
		"datasource": "prometheus",
		"queries": []map[string]any{{
			"refId":      "A",
			"datasource": map[string]string{"type": "prometheus", "uid": "prometheus"},
			"expr":       expr,
			"range":      true,
		}},
		"range": map[string]string{"from": "now-6h", "to": "now"},
	}
	panes, err := json.Marshal(map[string]any{"ale": pane})
	if err != nil {
		return ""
	}
	link := "https://factory-grafana.jomcgi.dev/explore?" + url.Values{
		"orgId": {"1"}, "schemaVersion": {"1"}, "panes": {string(panes)},
	}.Encode()
	if len(link) > 2048 {
		return ""
	}
	return link
}

func message(a alert, graph bool) embed {
	summary := a.Annotations["summary"]
	if summary == "" {
		summary = a.Labels["alertname"]
	}
	if a.Status == "resolved" {
		suffix := fmt.Sprintf(" · resolved after %s", a.EndsAt.Sub(a.StartsAt).Round(time.Second))
		return embed{Title: clip("🟢 "+summary, 256-len([]rune(suffix))) + suffix, Color: 0x57f287}
	}
	e := embed{Title: "🟠 " + summary, URL: exploreURL(a.Annotations["graph"]), Color: 0xf39c12}
	if a.Labels["severity"] == "critical" {
		e.Title = "🔴 " + summary
		e.Color = 0xe74c3c
	}
	e.Title = clip(e.Title, 256)
	started := fmt.Sprintf("Started <t:%d:R>", a.StartsAt.Unix())
	budget := 4096 - len(started) - 1
	var lines []string
	runbook := "[Runbook](" + a.Annotations["runbook"] + ")"
	linkLen := len(utf16.Encode([]rune(runbook)))
	includeLink := a.Annotations["runbook"] != "" && linkLen < budget
	if includeLink {
		budget -= linkLen + 1
	}
	if v := a.Annotations["action"]; v != "" {
		action := clip(v, budget)
		lines = append(lines, action)
		budget -= len(utf16.Encode([]rune(action))) + 1
	}
	if detail := a.Annotations["description"]; detail != "" && budget > 0 {
		lines = append(lines, clip(detail, budget))
	}
	if includeLink {
		lines = append(lines, runbook)
	}
	lines = append(lines, started)
	e.Description = strings.Join(lines, "\n")
	if graph {
		e.Image = map[string]string{"url": "attachment://graph.png"}
	}
	return e
}

type relay struct {
	client                  *http.Client
	prometheus, webhookFile string
}

func (r relay) deliver(ctx context.Context, a alert) error {
	var png []byte
	if a.Status != "resolved" && a.Annotations["graph"] != "" {
		var err error
		png, err = r.graph(ctx, a, time.Now())
		if err != nil {
			log.Print("graph unavailable: ", err)
			png = nil
		}
	}
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	payload, err := json.Marshal(map[string]any{"embeds": []embed{message(a, len(png) > 0)}})
	if err != nil {
		return err
	}
	if err = w.WriteField("payload_json", string(payload)); err != nil {
		return err
	}
	if len(png) > 0 {
		part, err := w.CreateFormFile("files[0]", "graph.png")
		if err != nil {
			return err
		}
		if _, err = part.Write(png); err != nil {
			return err
		}
	}
	if err = w.Close(); err != nil {
		return err
	}
	webhook, err := os.ReadFile(r.webhookFile)
	if err != nil {
		return errors.New("cannot read Discord webhook file")
	}
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimSpace(string(webhook)), &body)
	if err != nil {
		return errors.New("invalid Discord URL")
	}
	req.Header.Set("Content-Type", w.FormDataContentType())
	resp, err := r.client.Do(req)
	if err != nil {
		return errors.New("Discord request failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("Discord returned %d", resp.StatusCode)
	}
	return nil
}

func (r relay) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method != "POST" {
		w.Header().Set("Allow", "POST")
		http.Error(w, "POST required", 405)
		return
	}
	var batch struct {
		Version string
		Alerts  []alert
	}
	err := json.NewDecoder(http.MaxBytesReader(w, req.Body, 8<<20)).Decode(&batch)
	if err != nil || batch.Version != "4" {
		http.Error(w, "expected Alertmanager v4 webhook", 400)
		return
	}
	for _, a := range batch.Alerts {
		if err := r.deliver(req.Context(), a); err != nil {
			log.Print(err)
			http.Error(w, "delivery failed", 502)
			return
		}
	}
	w.WriteHeader(http.StatusNoContent)
}

func main() {
	addr := flag.String("listen", ":8080", "listen address")
	prom := flag.String("prometheus-url", "http://prometheus:9090", "Prometheus base URL")
	file := flag.String("discord-webhook-file", "/etc/alertmanager/discord/webhook-url", "Discord webhook URL file")
	flag.Parse()
	r := relay{&http.Client{Timeout: 15 * time.Second}, *prom, *file}
	server := http.Server{Addr: *addr, Handler: r, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(server.ListenAndServe())
}
