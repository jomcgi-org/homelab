package github

import (
	"context"
	"database/sql"
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

func (b *BrokerRuns) BindReviewTask(ctx context.Context, task provenance.ReviewerTask) error {
	client := BrokerComments{URL: b.URL, Path: provenance.BindReviewerTaskPath, Bearer: b.Bearer, HTTP: b.HTTP}
	return client.do(ctx, http.MethodPost, "", task, &struct{}{})
}

func (b *BrokerRuns) ReviewTaskMain(ctx context.Context, task provenance.ReviewerTask) (string, error) {
	client := BrokerComments{URL: b.URL, Path: provenance.ReadReviewerPinPath, Bearer: b.Bearer, HTTP: b.HTTP}
	var out struct {
		Main string `json:"main_sha"`
	}
	if err := client.do(ctx, http.MethodPost, "", task, &out); err != nil {
		return "", err
	}
	if out.Main == "" {
		return "", sql.ErrNoRows
	}
	return out.Main, nil
}
