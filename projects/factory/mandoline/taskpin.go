package mandoline

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"path/filepath"
	"strconv"
	"time"
)

func readAssignedMain(ctx context.Context, endpoint string) (string, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	defer transport.CloseIdleConnections()
	client := &http.Client{
		Transport: transport,
		Timeout:   5 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return "", err
	}
	// Substrate's egress gateway replaces the placeholder with the actor JWT.
	req.Header.Set("Authorization", "Bearer factory-task-pin")
	res, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return "", errors.New("task pin request refused")
	}
	body, err := io.ReadAll(io.LimitReader(res.Body, 1025))
	if err != nil || len(body) > 1024 {
		return "", errors.New("invalid task pin response size")
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var pin struct {
		MainSHA string `json:"main_sha"`
	}
	if err := decoder.Decode(&pin); err != nil || !fullSHA.MatchString(pin.MainSHA) {
		return "", errors.New("invalid task pin response")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return "", errors.New("unexpected data after task pin")
	}
	return pin.MainSHA, nil
}

// Fetch only the assigned commit and its commit history when the checkout
// lacks it. A temporary object store keeps the source checkout read-only.
func ensureMainObject(ctx context.Context, root, home, remote, main string, env []string) ([]string, error) {
	if capture(ctx, "/usr/bin/git", root, env, []string{"cat-file", "-e", main + "^{commit}"}).err == nil {
		return env, nil
	}
	store := filepath.Join(home, "main-objects")
	isolated := append(append([]string(nil), env...), "GIT_TERMINAL_PROMPT=0", "GIT_CEILING_DIRECTORIES="+filepath.Dir(home))
	if capture(ctx, "/usr/bin/git", home, isolated, []string{"init", "--bare", "--quiet", store}).err != nil {
		return nil, errors.New("cannot create a temporary main object store")
	}
	args := []string{
		"-c", "http.followRedirects=false", "-c", "remote.task-pin.url=" + remote,
		"fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
		"--filter=tree:0", "--", "task-pin", main,
	}
	if capture(ctx, "/usr/bin/git", store, isolated, args).err != nil {
		return nil, errors.New("the assigned main commit is unavailable")
	}
	combined := append(append([]string(nil), env...), "GIT_ALTERNATE_OBJECT_DIRECTORIES="+strconv.Quote(filepath.Join(store, "objects")))
	if capture(ctx, "/usr/bin/git", root, combined, []string{"cat-file", "-e", main + "^{commit}"}).err != nil {
		return nil, errors.New("cannot verify the assigned main object")
	}
	return combined, nil
}
