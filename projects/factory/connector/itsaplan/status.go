package itsaplan

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

// IssueSummary deliberately cannot hold an issue body or its activity.
type IssueSummary struct {
	ChoiceRevision string   `json:"choiceRevision,omitempty"`
	Choices        []string `json:"choices,omitempty"`
	ID             int64    `json:"id"`
	Identifier     string   `json:"identifier"`
	Title          string   `json:"title"`
	Priority       *string  `json:"priority"`
	CreatedAt      string   `json:"createdAt"`
	ColumnID       int64    `json:"columnId"`
	AssigneeUserID *string  `json:"assigneeUserId"`
	DelegateUserID *string  `json:"delegateUserId"`
	Archived       bool     `json:"archived"`
}

// ErrNotMember means the bound project has no member with the task owner's
// username. The status snapshot then marks only Needs you unavailable.
var ErrNotMember = errors.New("itsaplan: task owner is not a project member")

// NeedsYou reads the bound project's scaffold and at most 500 assigned issues
// as the coordinator. v1.10.0 has no null-delegate or open-state list filter,
// so those predicates are checked locally. A full page fails rather than
// presenting an incomplete count as all of Joe's tasks.
// The pinned upstream routes and schemas are linked in factory/ARCHITECTURE.md,
// "Factory front page". Resolve the member each time instead of pinning a user ID.
func (c *Client) NeedsYou(ctx context.Context, project, username string) ([]IssueSummary, error) {
	scaffold, err := c.GetProject(ctx, project)
	if err != nil {
		return nil, err
	}
	userID := scaffold.memberID(username)
	if userID == "" {
		return nil, ErrNotMember
	}
	path := "/projects/" + url.PathEscape(project)
	q := url.Values{"assigneeUserId": {userID}, "limit": {"500"}, "includeArchived": {"false"}}
	var issues []IssueSummary
	if err := c.do(ctx, c.Coordinator, http.MethodGet, path+"/issues?"+q.Encode(), nil, &issues); err != nil {
		return nil, err
	}
	if len(issues) >= 500 {
		return nil, fmt.Errorf("itsaplan: assigned issue listing reached its limit")
	}
	out := make([]IssueSummary, 0, len(issues))
	for _, issue := range issues {
		if issue.AssigneeUserID != nil && *issue.AssigneeUserID == userID && issue.DelegateUserID == nil && !issue.Archived && scaffold.openColumn(issue.ColumnID) {
			detail, err := c.GetIssue(ctx, issue.ID)
			if err != nil {
				return nil, err
			}
			if detail.AssigneeUserID != userID || detail.DelegateUserID != nil || detail.ArchivedAt != nil || !scaffold.openColumn(detail.ColumnID) || detail.Identifier != issue.Identifier {
				continue
			}
			issue.Choices = parseChoices(detail.Description)
			issue.ChoiceRevision = choiceRevision(issue.Choices)
			out = append(out, issue)
		}
	}
	return out, nil
}

// GetIssueSummary reads only the fields used by the factory front page.
func (c *Client) GetIssueSummary(ctx context.Context, id int64) (*IssueSummary, error) {
	var out IssueSummary
	if err := c.do(ctx, c.Coordinator, http.MethodGet, "/issues/"+strconv.FormatInt(id, 10), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// RecentIssueTitles reads one page of recently updated titles. Callers can
// look up individual issues outside the page by ID.
func (c *Client) RecentIssueTitles(ctx context.Context, project string) (map[int64]string, error) {
	q := url.Values{"limit": {"500"}, "includeArchived": {"true"}}
	path := "/projects/" + url.PathEscape(project) + "/issues?" + q.Encode()
	var issues []struct {
		ID    int64  `json:"id"`
		Title string `json:"title"`
	}
	if err := c.do(ctx, c.Coordinator, http.MethodGet, path, nil, &issues); err != nil {
		return nil, err
	}
	titles := make(map[int64]string, len(issues))
	for _, issue := range issues {
		titles[issue.ID] = issue.Title
	}
	return titles, nil
}

func (p *Project) memberID(username string) string {
	for _, member := range p.Assignees {
		if member.Kind == "member" && member.Username == username {
			return member.UserID
		}
	}
	return ""
}

func (p *Project) openColumn(id int64) bool {
	for _, col := range p.Columns {
		if col.ID == id {
			return col.StateType == "backlog" || col.StateType == "unstarted" || col.StateType == "started"
		}
	}
	return false
}

// The digest binds the decision to every displayed label and its position.
func choiceRevision(choices []string) string {
	if len(choices) == 0 {
		return ""
	}
	data, _ := json.Marshal(choices)
	return fmt.Sprintf("%x", sha256.Sum256(data))
}

// invariant: FACT-13
func parseChoices(description string) []string {
	var choices []string
	found, active := false, false
	fence := ""
	// html is set inside a raw HTML block: its closing marker, or "" for a
	// block that a blank line ends. Markdown inside it is not rendered.
	html, inHTML := "", false
	// para is set while a paragraph is open. A paragraph continues across
	// an indented line and an HTML block of type 7 cannot interrupt it.
	para := false
	setextEligible := false
	for _, raw := range strings.Split(description, "\n") {
		line := strings.TrimSpace(raw)
		if inHTML {
			inHTML = html == "" && line != "" || html != "" && !strings.Contains(strings.ToLower(line), html)
			continue
		}
		if fence == "" && indentWidth(raw) < 4 {
			if end, ok := htmlBlockEnd(line, para); ok {
				if active {
					return nil
				}
				inHTML = end == "" || !strings.Contains(strings.ToLower(line)[1:], end)
				html, para = end, false
				continue
			}
		}
		// Four columns of indentation start an indented code block, which
		// holds no heading or list item. Skip it outside a fenced block too.
		if fence == "" && line != "" && indentWidth(raw) >= 4 {
			if active {
				return nil
			}
			continue
		}
		if fence != "" {
			// A closing fence has at most three columns of indentation.
			if indentWidth(raw) < 4 && strings.HasPrefix(line, fence) && strings.Trim(line, fence[:1]) == "" {
				fence = ""
			}
			continue
		}
		if strings.HasPrefix(line, "```") || strings.HasPrefix(line, "~~~") {
			if active {
				return nil
			}
			fence, para = line[:len(line)-len(strings.TrimLeft(line, line[:1]))], false
			continue
		}
		marker := strings.TrimRight(strings.TrimLeft(raw, " "), " \t\r")
		// A setext underline completes the preceding top-level paragraph.
		// A thematic break also closes the paragraph, including after a list.
		setext := para && setextEligible && setextUnderline.MatchString(marker)
		para = line != "" && !strings.HasPrefix(line, "#") && !setext && !thematicBreak.MatchString(marker)
		setextEligible = para && !containerStart.MatchString(marker)
		if line == "## Choices" {
			if found {
				return nil
			}
			found, active = true, true
			continue
		}
		if strings.HasPrefix(line, "#") {
			active = false
		}
		if !active || line == "" {
			continue
		}
		// A choice starts at column 0. An indented item can nest under the
		// previous one, so the section is not a flat list.
		if indentWidth(raw) > 0 {
			return nil
		}
		label, ok := strings.CutPrefix(line, strconv.Itoa(len(choices)+1)+". ")
		if !ok || len(choices) == 4 {
			return nil
		}
		label, recommended := strings.CutSuffix(label, "(Recommended)")
		label = strings.TrimSpace(label)
		if recommended != (len(choices) == 0) || label == "" || strings.Contains(label, "(Recommended)") {
			return nil
		}
		choices = append(choices, label)
	}
	if len(choices) < 2 {
		return nil
	}
	return choices
}

var (
	containerStart  = regexp.MustCompile(`^(?:>|(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$))`)
	setextUnderline = regexp.MustCompile(`^(?:-+|=+)$`)
	thematicBreak   = regexp.MustCompile(`^(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$`)
)

// indentWidth counts leading columns, with a tab advancing to the next
// multiple of four as in CommonMark.
func indentWidth(line string) int {
	width := 0
	for _, r := range line {
		switch r {
		case ' ':
			width++
		case '\t':
			width += 4 - width%4
		default:
			return width
		}
	}
	return width
}

// htmlBlockTags are the tag names that start a CommonMark HTML block of
// type 6.
var htmlBlockTags = map[string]bool{}

func init() {
	for _, tag := range strings.Fields(`address article aside base basefont
		blockquote body caption center col colgroup dd details dialog dir div
		dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3
		h4 h5 h6 head header hr html iframe legend li link main menu menuitem
		nav noframes ol optgroup option p param search section summary table
		tbody td tfoot th thead title tr track ul`) {
		htmlBlockTags[tag] = true
	}
}

// htmlTagLine matches a line that holds only one complete open or closing
// tag: the start of a CommonMark HTML block of type 7. An autolink such as
// <https://example.com> does not match, because ":" cannot follow a tag
// name.
var htmlTagLine = regexp.MustCompile(`^(?:<[A-Za-z][A-Za-z0-9-]*` +
	`(?:[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*(?:[ \t]*=[ \t]*(?:[^ \t"'=<>` + "`" + `]+|'[^']*'|"[^"]*"))?)*` +
	`[ \t]*/?>|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)$`)

// htmlBlockEnd reports whether a line starts a CommonMark HTML block and
// returns its closing marker; "" means a blank line ends the block. para
// tells whether a paragraph is open, which a type 7 block cannot interrupt.
func htmlBlockEnd(line string, para bool) (string, bool) {
	lower := strings.ToLower(line)
	if !strings.HasPrefix(lower, "<") {
		return "", false
	}
	for _, tag := range []string{"pre", "script", "style", "textarea"} {
		if rest, ok := strings.CutPrefix(lower, "<"+tag); ok && (rest == "" || strings.ContainsAny(rest[:1], " \t>")) {
			return "</" + tag + ">", true
		}
	}
	switch {
	case strings.HasPrefix(lower, "<!--"):
		return "-->", true
	case strings.HasPrefix(lower, "<?"):
		return "?>", true
	case strings.HasPrefix(lower, "<![cdata["):
		return "]]>", true
	case strings.HasPrefix(lower, "<!") && len(lower) > 2 && lower[2] >= 'a' && lower[2] <= 'z':
		return ">", true
	}
	rest := strings.TrimPrefix(lower[1:], "/")
	name := strings.TrimLeft(rest, "abcdefghijklmnopqrstuvwxyz0123456789")
	name = rest[:len(rest)-len(name)]
	after := rest[len(name):]
	if htmlBlockTags[name] && (after == "" || after[0] == ' ' || after[0] == '\t' || after[0] == '>' || strings.HasPrefix(after, "/>")) {
		return "", true
	}
	if !para && htmlTagLine.MatchString(line) {
		return "", true
	}
	return "", false
}
