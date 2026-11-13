package warnings

import (
	"bytes"
	"context"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type rss struct {
	XMLName  xml.Name `xml:"rss"`
	Version  string   `xml:"version,attr"`
	Channels []struct {
		Title       string `xml:"title"`
		Link        string `xml:"link"`
		Description string `xml:"description"`
		Published   string `xml:"pubDate"`
		Items       []struct {
			Title       string `xml:"title"`
			Description string `xml:"description"`
			Link        string `xml:"link"`
		} `xml:"item"`
	} `xml:"channel"`
}

// ParseFeed distinguishes an observed empty channel from malformed XML. Unknown
// item metadata remains in the snapshot and makes assessment unknown.
func ParseFeed(body []byte, code string) ([]Item, error) {
	return parseFeed(body, regionNames[code])
}

func parseFeed(body []byte, name string) ([]Item, error) {
	if len(body) > MaxFeedBytes || name == "" {
		return nil, fmt.Errorf("warnings: feed bound/region")
	}
	// Bound nesting and node count before unmarshalling; encoding/xml never
	// resolves external entities. Reject directives and non-XML instructions.
	d := xml.NewDecoder(bytes.NewReader(body))
	depth, n := 0, 0
	var stack []string
	roots := 0
	seenFields := map[string]bool{}
	seenChannel := map[string]bool{}
	for {
		t, e := d.Token()
		if e == io.EOF {
			break
		}
		if e != nil {
			return nil, e
		}
		n++
		if n > 20000 {
			return nil, fmt.Errorf("warnings: too many XML tokens")
		}
		switch v := t.(type) {
		case xml.StartElement:
			if v.Name.Space != "" && !(v.Name.Local == "creator" && v.Name.Space == "http://purl.org/dc/elements/1.1/") {
				return nil, fmt.Errorf("warnings: unexpected namespace")
			}
			parent := ""
			if len(stack) > 0 {
				parent = stack[len(stack)-1]
			}
			allowed := map[string]string{"": " rss ", "rss": " channel ", "channel": " title link description language copyright pubDate creator item ", "item": " title link description guid enclosure "}
			if !strings.Contains(allowed[parent], " "+v.Name.Local+" ") {
				return nil, fmt.Errorf("warnings: unexpected XML element")
			}
			if parent == "" {
				roots++
				if roots != 1 {
					return nil, fmt.Errorf("warnings: multiple roots")
				}
			}
			if parent == "channel" && v.Name.Local != "item" {
				if seenChannel[v.Name.Local] {
					return nil, fmt.Errorf("warnings: duplicate channel field")
				}
				seenChannel[v.Name.Local] = true
			}
			if v.Name.Local == "item" {
				seenFields = map[string]bool{}
			}
			if parent == "item" {
				if seenFields[v.Name.Local] {
					return nil, fmt.Errorf("warnings: duplicate item field")
				}
				seenFields[v.Name.Local] = true
			}
			stack = append(stack, v.Name.Local)
			depth++
			if depth > 12 {
				return nil, fmt.Errorf("warnings: XML depth")
			}
		case xml.EndElement:
			stack = stack[:len(stack)-1]
			depth--
		case xml.CharData:
			if (depth == 0 || (len(stack) > 0 && (stack[len(stack)-1] == "rss" || stack[len(stack)-1] == "channel" || stack[len(stack)-1] == "item"))) && strings.TrimSpace(string(v)) != "" {
				return nil, fmt.Errorf("warnings: unexpected XML text")
			}
		case xml.Directive:
			return nil, fmt.Errorf("warnings: XML directive")
		case xml.ProcInst:
			if v.Target != "xml" {
				return nil, fmt.Errorf("warnings: XML instruction")
			}
		}
	}
	var f rss
	if e := xml.Unmarshal(body, &f); e != nil {
		return nil, e
	}
	if f.Version != "2.0" || len(f.Channels) != 1 {
		return nil, fmt.Errorf("warnings: not one RSS 2 channel")
	}
	c := f.Channels[0]
	if c.Title != "Met Office warnings for "+name || c.Link != OfficialURL || c.Description != "Weather warnings of severe and extreme weather from the Met Office" || len(c.Items) > MaxItems {
		return nil, fmt.Errorf("warnings: unexpected channel")
	}
	if _, e := http.ParseTime(c.Published); e != nil {
		return nil, fmt.Errorf("warnings: missing channel issue date")
	}
	out := make([]Item, 0, len(c.Items))
	for _, raw := range c.Items {
		i := Item{raw.Title, raw.Description, raw.Link}
		if e := validateItem(i); e != nil {
			return nil, e
		}
		out = append(out, i)
	}
	return out, nil
}

// Collect fetches exactly six fixed public destinations, with no credentials,
// redirects, revalidation or configurable upstream URL. A failure emits a region
// with Complete=false and no source clock. Every successful read is a full 200.
func Collect(ctx context.Context, transport http.RoundTripper, now func() time.Time) *Snapshot {
	client := &http.Client{Transport: transport, Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	s := &Snapshot{Version: 1}
	for _, code := range Regions {
		r := Region{Code: code}
		func() {
			req, e := http.NewRequestWithContext(ctx, http.MethodGet, FeedBase+code, nil)
			if e != nil {
				return
			}
			req.Header.Set("User-Agent", "scotland-connector-warnings/1 (+https://jomcgi.dev/scotland/v1/method)")
			req.Header.Set("Cache-Control", "no-cache")
			resp, e := client.Do(req)
			if e != nil {
				return
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				return
			}
			// Reject known intermediary cache age: collection must observe upstream.
			if age := resp.Header.Get("Age"); age != "" && age != "0" {
				return
			}
			b, e := io.ReadAll(io.LimitReader(resp.Body, MaxFeedBytes+1))
			if e != nil {
				return
			}
			items, e := ParseFeed(b, code)
			if e != nil {
				return
			}
			r.Items, r.Complete, r.CheckedAt = items, true, now().UTC()
		}()
		s.Regions = append(s.Regions, r)
	}
	s.CreatedAt = now().UTC()
	return s
}
