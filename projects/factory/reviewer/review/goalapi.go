package review

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// The goal reasons API gives the delivery owner the goal check's private
// reasons for a head (HOME-251). Only the connector reaches it: it listens
// apart from /metrics, and its network policy admits the connector alone.
// The connector posts the reasons on the private issue and quotes them, as
// untrusted data, in the coder's next revision prompt. No reason reaches a
// check, a PR comment or a log.

// GoalReasonsPath is the API's one route.
const GoalReasonsPath = "/v1/goal-reasons"

// maxGoalReasons bounds the reasons one answer carries.
const maxGoalReasons = 4000

// GoalReasons is the selected goal check reply for one head: the reasons
// before its JSON answer, with the issue and evidence digest it judged.
type GoalReasons struct {
	Repository  string   `json:"repository"`
	PullRequest int      `json:"pullRequest"`
	Head        string   `json:"head"`
	Issue       string   `json:"issue"`
	Digest      string   `json:"digest"`
	Phase       string   `json:"phase"`
	Findings    []string `json:"findings,omitempty"`
	Reasons     string   `json:"reasons"`
}

var (
	auditHash = regexp.MustCompile(`^[0-9a-f]{64}$`)
	headSHA   = regexp.MustCompile(`^[0-9a-f]{40}$`)
	repoName  = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
)

// invariant: FACT-13
func (s *FileGoalAudit) reasons(repo string, pr int, head, issue string, selected *GoalCheck) (GoalReasons, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !auditHash.MatchString(selected.AuditID) || selected.Digest == "" {
		return GoalReasons{}, false, nil
	}
	data, err := os.ReadFile(filepath.Join(s.dir, selected.AuditID+".json"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			if _, dirErr := os.Stat(s.dir); dirErr == nil {
				return GoalReasons{}, false, nil
			}
		}
		return GoalReasons{}, false, err
	}
	var best GoalAuditRecord
	if json.Unmarshal(data, &best) != nil || best.Repository != repo || best.PullRequest != pr || best.Head != head || best.Issue != issue || best.Digest != selected.Digest || auditID(best.MessageID, best.TaskID) != selected.AuditID {
		return GoalReasons{}, false, nil
	}
	reasons := strings.TrimSpace(best.Ownership + replyReasons(best.Reply))
	if reasons == "" {
		return GoalReasons{}, false, nil
	}
	var findings []string
	if selected.Overall == goalNotMet {
		for _, line := range strings.Split(reasons, "\n") {
			if strings.HasPrefix(strings.TrimSpace(line), "Overall: ") {
				findings = append(findings, clip(strings.TrimSpace(line), 400))
				break
			}
		}
		// Older replies explain the overall result in unlabelled prose.
		if len(findings) == 0 {
			for _, line := range strings.Split(reasons, "\n") {
				if detail := strings.TrimSpace(line); detail != "" && !strings.HasPrefix(detail, "Criterion ") {
					findings = append(findings, "Overall: "+clip(detail, 400))
					break
				}
			}
		}
	}
	for _, c := range selected.Criteria {
		if c.Verdict != "not_met" {
			continue
		}
		prefix := "Criterion " + strconv.Itoa(c.Criterion) + ": "
		reason := "The implementation does not demonstrate this criterion."
		for _, line := range strings.Split(reasons, "\n") {
			if detail, ok := strings.CutPrefix(strings.TrimSpace(line), prefix); ok && detail != "" {
				reason = clip(detail, 160)
				break
			}
		}
		findings = append(findings, prefix+reason)
	}
	return GoalReasons{
		Findings: findings, Repository: best.Repository, PullRequest: best.PullRequest, Head: best.Head,
		Issue: best.Issue, Digest: best.Digest, Phase: best.Phase, Reasons: clip(reasons, maxGoalReasons),
	}, true, nil
}

// replyReasons is a reply without its last fenced block, the answer.
func replyReasons(reply string) string {
	if m := fence.FindAllStringIndex(reply, -1); len(m) > 0 {
		last := m[len(m)-1]
		reply = reply[:last[0]] + reply[last[1]:]
	}
	return reply
}

// GoalReasonsSource is what the API reads.
type GoalReasonsSource interface {
	Reasons(ctx context.Context, repo string, pr int, head string) (GoalReasons, bool, error)
}

// GoalReasonsHandler serves GET GoalReasonsPath?repository=o/r&pullRequest=N&head=SHA.
// It answers 404 when no reply is kept for that head, and never logs a
// query's answer.
func GoalReasonsHandler(src GoalReasonsSource) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != GoalReasonsPath {
			http.NotFound(w, r)
			return
		}
		q := r.URL.Query()
		repo, head := q.Get("repository"), q.Get("head")
		pr, err := strconv.Atoi(q.Get("pullRequest"))
		if err != nil || pr <= 0 || !repoName.MatchString(repo) || !headSHA.MatchString(head) {
			http.Error(w, "bad query", http.StatusBadRequest)
			return
		}
		got, ok, err := src.Reasons(r.Context(), repo, pr, head)
		if err != nil {
			http.Error(w, "goal reasons unavailable", http.StatusServiceUnavailable)
			return
		}
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(got)
	})
}
