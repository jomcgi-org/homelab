// Command decisionprobe submits one pick to the connector handler against the
// private itsaplan and connector databases created by the integration test.
package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func run() error {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	plan := &itsaplan.Client{
		BaseURL:     os.Getenv("TEST_PLAN_URL"),
		HTTP:        &http.Client{Timeout: 10 * time.Second},
		Coordinator: "coordinator",
		Keys:        map[string]string{"coordinator": os.Getenv("TEST_READ_KEY")},
	}
	issues, err := plan.NeedsYou(ctx, "HOME", "joe")
	if err != nil || len(issues) != 1 || len(issues[0].Choices) != 2 || issues[0].Choices[1] != "Wait" {
		return fmt.Errorf("expected one handoff with Wait as choice 2")
	}
	st, err := store.Open(os.Getenv("FACTORY_CONNECTOR_DATABASE_URL"))
	if err != nil {
		return fmt.Errorf("cannot open fixture database")
	}
	defer st.DB.Close()
	h := itsaplan.DecisionHandler{
		Plan:      plan,
		Projects:  []string{"HOME"},
		Origin:    os.Getenv("APP_URL"),
		WithFence: st.WithDecisionFence,
		Password:  func() (string, error) { return os.Getenv("TEST_SESSION_PASSWORD"), nil },
	}
	req := httptest.NewRequest("POST", "/decide", strings.NewReader(fmt.Sprintf(`{"key":%q,"choice":2}`, issues[0].Identifier))).WithContext(ctx)
	req.Header.Set("If-Match", strconv.Quote(issues[0].ChoiceRevision))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 204 {
		return fmt.Errorf("decision returned HTTP %d", w.Code)
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
