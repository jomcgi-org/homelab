package github

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// MaxRecentPullPages bounds a listing of PRs by last update: 3000 PRs.
const MaxRecentPullPages = 30

// UpdatedPullRequest is a PR from a listing by last update. ClosedAt is
// zero while it is open.
type UpdatedPullRequest struct {
	PullRequest
	ClosedAt time.Time
}

type updatedPull struct {
	restPull
	ClosedAt *time.Time `json:"closed_at"`
}

func (p updatedPull) pr() UpdatedPullRequest {
	out := UpdatedPullRequest{PullRequest: *p.restPull.pr()}
	if p.ClosedAt != nil {
		out.ClosedAt = *p.ClosedAt
	}
	return out
}

// PullRequestsUpdatedSince lists PRs in every state and into every base
// whose last update is at or after since, most recently updated first.
// complete is false when MaxRecentPullPages full pages end before since.
// Page 1 is read again at the end: a PR updated during the read moves there
// and would otherwise slip past the pages already read. A PR can appear
// twice.
func (c *Client) PullRequestsUpdatedSince(ctx context.Context, repo string, since time.Time) (prs []UpdatedPullRequest, complete bool, err error) {
	read := func(page int) ([]updatedPull, error) {
		q := url.Values{
			"state": {"all"}, "sort": {"updated"}, "direction": {"desc"},
			"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)},
		}
		var pulls []updatedPull
		if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/pulls?"+q.Encode(), nil, &pulls); err != nil {
			return nil, err
		}
		if pulls == nil {
			return nil, errors.New("github: missing PR listing")
		}
		for _, p := range pulls {
			if p.UpdatedAt == nil || p.UpdatedAt.IsZero() || p.Head.SHA == "" || (p.State != "open" && (p.ClosedAt == nil || p.ClosedAt.IsZero())) {
				return nil, errors.New("github: incomplete PR in listing")
			}
		}
		return pulls, nil
	}
	for page := 1; page <= MaxRecentPullPages && !complete; page++ {
		pulls, err := read(page)
		if err != nil {
			return nil, false, err
		}
		for _, p := range pulls {
			prs = append(prs, p.pr())
		}
		complete = len(pulls) < MaxListed || pulls[len(pulls)-1].UpdatedAt.Before(since)
	}
	if !complete {
		return prs, false, nil
	}
	pulls, err := read(1)
	if err != nil {
		return nil, false, err
	}
	for _, p := range pulls {
		prs = append(prs, p.pr())
	}
	return prs, true, nil
}

// MaxCommitPullPages bounds the PRs listed for one commit: 1000.
const MaxCommitPullPages = 10

// CommitPullRequests lists the PRs GitHub associates with a commit. For a
// commit outside the default branch these are the open PRs, into any base,
// whose commits include it; closed PRs are not listed. complete is false
// past MaxCommitPullPages full pages.
func (c *Client) CommitPullRequests(ctx context.Context, repo, sha string) (prs []PullRequest, complete bool, err error) {
	for page := 1; page <= MaxCommitPullPages; page++ {
		q := url.Values{"per_page": {strconv.Itoa(MaxListed)}, "page": {strconv.Itoa(page)}}
		var pulls []restPull
		if err := c.do(ctx, repo, http.MethodGet, repoPath(repo)+"/commits/"+url.PathEscape(sha)+"/pulls?"+q.Encode(), nil, &pulls); err != nil {
			return nil, false, err
		}
		if pulls == nil {
			return nil, false, errors.New("github: missing commit PR listing")
		}
		for _, p := range pulls {
			if p.Number <= 0 || p.Head.SHA == "" || p.Base.Ref == "" {
				return nil, false, errors.New("github: incomplete PR in commit listing")
			}
			prs = append(prs, *p.pr())
		}
		if len(pulls) < MaxListed {
			return prs, true, nil
		}
	}
	return prs, false, nil
}
