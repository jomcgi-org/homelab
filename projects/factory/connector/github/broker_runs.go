package github

import (
	"context"
	"net/http"

	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
)

const ReviewerRunsPath = "/runs/reviewer"

type BrokerRuns struct {
	URL    string
	Bearer Bearer
	HTTP   *http.Client
}

func (b *BrokerRuns) RecordReview(ctx context.Context, run provenance.ReviewerRun) error {
	client := BrokerComments{URL: b.URL, Path: ReviewerRunsPath, Bearer: b.Bearer, HTTP: b.HTTP}
	var out struct {
		ID int64 `json:"id"`
	}
	return client.do(ctx, http.MethodPost, "", run, &out)
}
