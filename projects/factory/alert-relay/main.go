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
	"os"
	"strings"
	"time"
)

type alert struct {
	Status              string
	Labels, Annotations map[string]string
	StartsAt, EndsAt    time.Time
}

type embed struct {
	Title       string            `json:"title"`
	Description string            `json:"description,omitempty"`
	Color       int               `json:"color"`
	Image       map[string]string `json:"image,omitempty"`
}

func message(a alert, graph bool) embed {
	summary := a.Annotations["summary"]
	if summary == "" {
		summary = a.Labels["alertname"]
	}
	if a.Status == "resolved" {
		return embed{Title: fmt.Sprintf("🟢 %s · resolved after %s", summary, a.EndsAt.Sub(a.StartsAt).Round(time.Second)), Color: 0x57f287}
	}
	e := embed{Title: "🟠 " + summary, Color: 0xf39c12}
	if a.Labels["severity"] == "critical" {
		e.Title = "🔴 " + summary
		e.Color = 0xe74c3c
	}
	var lines []string
	if v := a.Annotations["action"]; v != "" {
		lines = append(lines, v)
	}
	if v := a.Annotations["runbook"]; v != "" {
		lines = append(lines, "[Runbook]("+v+")")
	}
	lines = append(lines, fmt.Sprintf("Started <t:%d:R>", a.StartsAt.Unix()))
	e.Description = strings.Join(lines, "\n")
	if graph {
		e.Image = map[string]string{"url": "attachment://graph.png"}
	}
	return e
}

type relay struct {
	client              *http.Client
	prometheus, discord string
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
	req, err := http.NewRequestWithContext(ctx, "POST", r.discord, &body)
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
	b, err := os.ReadFile(*file)
	if err != nil {
		log.Fatal(err)
	}
	r := relay{&http.Client{Timeout: 15 * time.Second}, *prom, strings.TrimSpace(string(b))}
	server := http.Server{Addr: *addr, Handler: r, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(server.ListenAndServe())
}
