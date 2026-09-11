package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"time"

	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
)

type githubCanaryConfig struct {
	brokerURL    string
	brokerID     spiffeid.ID
	grant        string
	deniedGrant  string
	repositoryID int64
}

func configuredGitHubCanary() (githubCanaryConfig, error) {
	c := githubCanaryConfig{brokerURL: os.Getenv("CANARY_BROKER_URL"), grant: os.Getenv("CANARY_GRANT"), deniedGrant: os.Getenv("CANARY_DENIED_GRANT")}
	endpoint, err := url.Parse(c.brokerURL)
	if err != nil || endpoint.Scheme != "https" || endpoint.Host == "" || endpoint.User != nil || endpoint.Path != "" || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return c, errors.New("CANARY_BROKER_URL must be an HTTPS origin")
	}
	c.brokerID, err = spiffeid.FromString(os.Getenv("CANARY_BROKER_SPIFFE_ID"))
	if err != nil {
		return c, errors.New("CANARY_BROKER_SPIFFE_ID must be a SPIFFE ID")
	}
	names := regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
	if !names.MatchString(c.grant) || !names.MatchString(c.deniedGrant) || c.grant == c.deniedGrant {
		return c, errors.New("canary requires distinct valid allowed and denied grants")
	}
	c.repositoryID, err = strconv.ParseInt(os.Getenv("CANARY_REPOSITORY_ID"), 10, 64)
	if err != nil || c.repositoryID <= 0 {
		return c, errors.New("CANARY_REPOSITORY_ID must be positive")
	}
	return c, nil
}

func runGitHubCanary(logger *slog.Logger) error {
	config, err := configuredGitHubCanary()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	source, err := newWorkloadX509Source(ctx)
	if err != nil {
		return errors.New("canary SPIFFE source unavailable")
	}
	defer source.Close()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.TLSClientConfig = tlsconfig.MTLSClientConfig(source, source, tlsconfig.AuthorizeID(config.brokerID))
	defer transport.CloseIdleConnections()
	noRedirect := func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	brokerClient := &http.Client{Transport: transport, Timeout: 20 * time.Second, CheckRedirect: noRedirect}
	githubTransport := http.DefaultTransport.(*http.Transport).Clone()
	githubTransport.Proxy = nil
	defer githubTransport.CloseIdleConnections()
	githubClient := &http.Client{Transport: githubTransport, Timeout: 15 * time.Second, CheckRedirect: noRedirect}
	if err := checkGitHubCanary(ctx, config, brokerClient, githubClient); err != nil {
		return err
	}
	logger.Info("GitHub canary passed", "repository_id", config.repositoryID, "grant", config.grant, "denied_grant", config.deniedGrant)
	return nil
}

// Response bodies and credentials never appear in errors or logs. The token
// remains in this process and is used only with GitHub's fixed API origin.
func checkGitHubCanary(ctx context.Context, c githubCanaryConfig, brokerClient, githubClient *http.Client) error {
	request := func(client *http.Client, endpoint, token string) (*http.Response, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return nil, errors.New("canary request invalid")
		}
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
			req.Header.Set("Accept", "application/vnd.github+json")
			req.Header.Set("X-GitHub-Api-Version", "2026-03-10")
		}
		response, err := client.Do(req)
		if err != nil {
			return nil, errors.New("canary request failed")
		}
		return response, nil
	}
	denied, err := request(brokerClient, c.brokerURL+"/github/grants/"+c.deniedGrant+"/token", "")
	if err != nil {
		return err
	}
	denied.Body.Close()
	if denied.StatusCode != http.StatusForbidden {
		return fmt.Errorf("canary denied-grant status %d, expected 403", denied.StatusCode)
	}
	allowed, err := request(brokerClient, c.brokerURL+"/github/grants/"+c.grant+"/token", "")
	if err != nil {
		return err
	}
	defer allowed.Body.Close()
	if allowed.StatusCode != http.StatusOK {
		return fmt.Errorf("canary token status %d", allowed.StatusCode)
	}
	var token struct {
		AccessToken string    `json:"access_token"`
		ExpiresAt   time.Time `json:"expires_at"`
	}
	if err := decodeCanaryJSON(allowed.Body, &token); err != nil || token.AccessToken == "" || !token.ExpiresAt.After(time.Now().Add(time.Minute)) {
		return errors.New("canary token response invalid or expiring")
	}
	repositories, err := request(githubClient, "https://api.github.com/installation/repositories?per_page=100", token.AccessToken)
	if err != nil {
		return err
	}
	defer repositories.Body.Close()
	if repositories.StatusCode != http.StatusOK {
		return fmt.Errorf("canary repository status %d", repositories.StatusCode)
	}
	var result struct {
		TotalCount   int `json:"total_count"`
		Repositories []struct {
			ID int64 `json:"id"`
		} `json:"repositories"`
	}
	if err := decodeCanaryJSON(repositories.Body, &result); err != nil {
		return errors.New("canary repository response invalid")
	}
	if result.TotalCount != 1 || len(result.Repositories) != 1 || result.Repositories[0].ID != c.repositoryID {
		return errors.New("canary token is not scoped to exactly the expected repository")
	}
	return nil
}

func decodeCanaryJSON(body io.Reader, out any) error {
	data, err := io.ReadAll(io.LimitReader(body, (1<<20)+1))
	if err != nil || len(data) > 1<<20 {
		return errors.New("invalid canary response size")
	}
	return json.Unmarshal(data, out)
}
