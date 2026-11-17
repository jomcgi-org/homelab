package itsaplan

import (
	"context"
	"testing"
)

func TestDeferredProjectAndBoardContract(t *testing.T) {
	var got seen
	c := server(t, 200, `{"project":{"id":7},"columns":[{"id":1,"name":"Backlog","stateType":"backlog"}],"labels":[{"id":8,"name":"deferred"}],"assignees":[{"userId":"coordinator-id","username":"coord","kind":"agent"}]}`, &got)
	p, err := c.Project(context.Background(), "7.HOME")
	if err != nil || p.Project.ID != 7 || p.Labels[0].ID != 8 || got.path != "/projects/7.HOME" || got.key != "k-coord" {
		t.Fatalf("project=%+v request=%+v err=%v", p, got, err)
	}
	c = server(t, 200, `{"issues":[{"id":118,"projectId":7,"columnId":1,"startDate":"2026-11-14","delegateUserId":null,"archivedAt":null,"labelIds":[8]},{"id":119,"delegateUserId":"another","archivedAt":"2026-11-01T00:00:00Z"}]}`, &got)
	issues, err := c.ProjectIssues(context.Background(), "7.HOME")
	if err != nil || len(issues) != 2 {
		t.Fatalf("issues=%+v err=%v", issues, err)
	}
	i := issues[0]
	if i.StartDate != "2026-11-14" || i.ProjectID != 7 || i.DelegateUserID != nil || i.ArchivedAt != nil || i.LabelIDs[0] != 8 || issues[1].DelegateUserID == nil || issues[1].ArchivedAt == nil {
		t.Fatalf("issues=%+v", issues)
	}
	if got.path != "/projects/7.HOME/issues/board" || got.key != "k-coord" {
		t.Fatalf("request=%+v", got)
	}
}

func TestDeferredTransactionContract(t *testing.T) {
	var got seen
	c := server(t, 200, `{"key":"deferred/118/2026-11-14","runId":42}`, &got)
	receipt, err := c.StartDeferred(context.Background(), 118, DeferredRequest{
		ProjectID: 7, StartDate: "2026-11-14", DelegateUserID: "coder-id",
	})
	if err != nil || receipt.Key != "deferred/118/2026-11-14" || receipt.RunID != 42 {
		t.Fatalf("receipt=%+v err=%v", receipt, err)
	}
	if got.method != "POST" || got.path != "/issues/118/factory-deferred-start" || got.key != "k-coord" ||
		len(got.body) != 3 || got.body["delegateUserId"] != "coder-id" || got.body["startDate"] != "2026-11-14" || got.body["projectId"] != float64(7) {
		t.Fatalf("request=%+v", got)
	}
}
