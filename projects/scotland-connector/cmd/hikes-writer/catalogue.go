package main

import (
	"fmt"
	"io"
	"log"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"golang.org/x/net/html"
)

func attr(n *html.Node, key string) string {
	for _, a := range n.Attr {
		if a.Key == key {
			return a.Val
		}
	}
	return ""
}

func inside(n *html.Node, key, value string) bool {
	for p := n; p != nil; p = p.Parent {
		for _, v := range strings.Fields(attr(p, key)) {
			if v == value {
				return true
			}
		}
	}
	return false
}

func content(n *html.Node) string {
	if n.Type == html.TextNode {
		return n.Data
	}
	var s strings.Builder
	for c := n.FirstChild; c != nil; c = c.NextSibling {
		s.WriteString(content(c))
	}
	return strings.TrimSpace(s.String())
}

var (
	coord     = regexp.MustCompile(`[-+]?\d+(?:\.\d+)?`)
	km        = regexp.MustCompile(`([\d.]+)\s*km`)
	metres    = regexp.MustCompile(`(\d+)\s*m`)
	duration  = regexp.MustCompile(`([\d.]+)\s*(hours?|hrs?|minutes?|mins?|days?)`)
	fractions = strings.NewReplacer("¼", ".25", "½", ".5", "¾", ".75")
)

func hours(s string) float64 {
	s = strings.Split(strings.Split(strings.ToLower(s), "/")[0], " or ")[0]
	parts := strings.Split(fractions.Replace(s), "-")
	var h float64
	for _, m := range duration.FindAllStringSubmatch(parts[len(parts)-1], -1) {
		v, _ := strconv.ParseFloat(m[1], 64)
		switch m[2][0] {
		case 'd':
			v *= 24
		case 'm':
			v /= 60
		}
		h += v
	}
	return h
}

func coordinate(v float64) string {
	s := strconv.FormatFloat(v, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

func page(r io.Reader, base *url.URL, level int) ([]string, walk, error) {
	doc, err := html.Parse(r)
	if err != nil {
		return nil, walk{}, err
	}
	var links []string
	var w walk
	haveCoords, haveDistance, haveAscent := false, false, false
	for n := range doc.Descendants() {
		if n.Type != html.ElementNode {
			continue
		}
		href := attr(n, "href")
		if level < 3 {
			if n.Data != "a" || href == "" {
				continue
			}
			switch level {
			case 0:
				if !inside(n, "id", "choosearea") || !inside(n, "class", "cell") || strings.Contains(href, ".shtml") || strings.Contains(href, ".php") {
					continue
				}
			case 1:
				if !inside(n, "id", "arealist") || !inside(n, "class", "cell") || strings.Contains(href, ".php") {
					continue
				}
			case 2:
				if !inside(n, "class", "walktable") || !inside(n, "class", "table1") || n.Parent.Data != "td" {
					continue
				}
				first := true
				for p := n.Parent.PrevSibling; p != nil; p = p.PrevSibling {
					if p.Type == html.ElementNode {
						first = false
					}
				}
				if !first {
					continue
				}
			}
			u, err := base.Parse(href)
			if err == nil && u.Host == base.Host && u.Scheme == "https" {
				links = append(links, u.String())
			}
			continue
		}
		switch n.Data {
		case "h1":
			if inside(n, "class", "wtitle") || inside(n, "class", "hero-title") || (w.Name == "" && inside(n, "id", "content")) {
				w.Name = content(n)
			}
		case "link":
			if attr(n, "rel") == "canonical" {
				w.URL = href
			}
		case "a":
			if strings.HasPrefix(href, "https://www.google.com/maps/search/") {
				m := coord.FindAllString(href, -1)
				if len(m) == 2 {
					w.Lat, _ = strconv.ParseFloat(m[0], 64)
					w.Lon, _ = strconv.ParseFloat(m[1], 64)
					haveCoords = true
				}
			}
		case "dt", "h2":
			if n.Data == "dt" && !inside(n, "id", "col") {
				continue
			}
			if n.Data == "h2" && !inside(n, "class", "route-main-col") {
				continue
			}
			value := n.NextSibling
			for value != nil && value.Type != html.ElementNode {
				value = value.NextSibling
			}
			if value == nil || (n.Data == "dt" && value.Data != "dd") || (n.Data == "h2" && value.Data != "div") {
				continue
			}
			label := strings.ToLower(content(n))
			if n.Data == "h2" && !inside(n.Parent, "class", label) {
				continue
			}
			v := strings.ToLower(content(value))
			switch {
			case strings.Contains(label, "distance"):
				if m := km.FindStringSubmatch(v); m != nil {
					w.Distance, err = strconv.ParseFloat(m[1], 64)
					haveDistance = err == nil
				}
			case strings.Contains(label, "ascent"):
				if m := metres.FindStringSubmatch(v); m != nil {
					w.Ascent, err = strconv.Atoi(m[1])
					haveAscent = err == nil
				}
			case strings.Contains(label, "time"):
				w.Duration = hours(v)
			}
		}
	}
	if level == 3 {
		if !haveCoords || !haveDistance || !haveAscent || w.Duration <= 0 || w.Name == "" || w.URL == "" {
			return nil, w, fmt.Errorf("incomplete walk")
		}
		w.ID = uuid.NewSHA1(uuid.NameSpaceURL, []byte(coordinate(w.Lat)+","+coordinate(w.Lon))).String()
	}
	return links, w, nil
}

func catalogue() ([]walk, error) {
	byID := map[string]walk{}
	urls := []string{"https://www.walkhighlands.co.uk/"}
	count := 0
	for level := 0; level <= 3; level++ {
		seen := map[string]bool{}
		var next []string
		for _, address := range urls {
			if seen[address] {
				continue
			}
			seen[address] = true
			base, err := url.Parse(address)
			if err != nil {
				return nil, err
			}
			err = fetch(address, func(r io.Reader) error {
				links, w, err := page(r, base, level)
				if err != nil {
					return err
				}
				next = append(next, links...)
				if level == 3 {
					byID[w.ID] = w
					count++
				}
				return nil
			})
			if err != nil {
				log.Printf("catalogue %s: %v", address, err)
			}
			time.Sleep(500 * time.Millisecond)
		}
		urls = next
	}
	if count == 0 {
		return nil, fmt.Errorf("no publishable routes fetched")
	}
	result := make([]walk, 0, len(byID))
	for _, w := range byID {
		result = append(result, w)
	}
	return result, nil
}
