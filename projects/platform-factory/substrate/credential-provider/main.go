// Command credential-provider is a kubelet image credential provider
// (credentialprovider.kubelet.k8s.io/v1) for Substrate's atelet, which pulls
// actor and workload images itself and so ignores pod imagePullSecrets
// (HOME-539).
//
// It answers each request with the registry's login from a mounted Docker
// config (a kubernetes.io/dockerconfigjson Secret), named by
// $DOCKER_CONFIG_FILE. "credential-provider install DEST" copies the binary
// to DEST, so an init container can fill atelet's plugin directory without a
// shell.
package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const (
	apiVersion    = "credentialprovider.kubelet.k8s.io/v1"
	cacheDuration = "5m0s"
)

type request struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Image      string `json:"image"`
}

type authConfig struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type response struct {
	APIVersion    string                `json:"apiVersion"`
	Kind          string                `json:"kind"`
	CacheKeyType  string                `json:"cacheKeyType"`
	CacheDuration string                `json:"cacheDuration"`
	Auth          map[string]authConfig `json:"auth"`
}

type dockerEntry struct {
	Auth     string `json:"auth"`
	Username string `json:"username"`
	Password string `json:"password"`
}

func main() {
	var err error
	if len(os.Args) == 3 && os.Args[1] == "install" {
		err = install(os.Args[2])
	} else {
		err = provide(os.Stdin, os.Stdout, os.Getenv("DOCKER_CONFIG_FILE"))
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "credential-provider:", err)
		os.Exit(1)
	}
}

// provide answers one CredentialProviderRequest. A registry missing from the
// config gets an empty answer, so the pull stays anonymous.
func provide(in io.Reader, out io.Writer, configPath string) error {
	var req request
	if err := json.NewDecoder(in).Decode(&req); err != nil {
		return fmt.Errorf("read request: %w", err)
	}
	if req.APIVersion != apiVersion || req.Kind != "CredentialProviderRequest" {
		return fmt.Errorf("unsupported request %s %s", req.APIVersion, req.Kind)
	}
	registry := registryOf(req.Image)
	if registry == "" {
		return fmt.Errorf("request has no image")
	}
	auths, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	resp := response{
		APIVersion:    apiVersion,
		Kind:          "CredentialProviderResponse",
		CacheKeyType:  "Registry",
		CacheDuration: cacheDuration,
		Auth:          map[string]authConfig{},
	}
	for _, key := range []string{registry, "https://" + registry} {
		entry, ok := auths[key]
		if !ok {
			continue
		}
		login, err := entry.login()
		if err != nil {
			// The error names the registry only, never the credential.
			return fmt.Errorf("config entry for %s: %w", registry, err)
		}
		resp.Auth[registry] = login
		break
	}
	return json.NewEncoder(out).Encode(resp)
}

// registryOf returns the registry host of an image reference. A reference
// without a host is Docker Hub's.
func registryOf(image string) string {
	if image == "" {
		return ""
	}
	host, _, found := strings.Cut(image, "/")
	if !found || !(strings.ContainsAny(host, ".:") || host == "localhost") {
		return "docker.io"
	}
	return host
}

func loadConfig(path string) (map[string]dockerEntry, error) {
	if path == "" {
		return nil, errors.New("DOCKER_CONFIG_FILE is not set")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read Docker config: %w", err)
	}
	var config struct {
		Auths map[string]dockerEntry `json:"auths"`
	}
	if err := json.Unmarshal(data, &config); err != nil {
		// json errors can quote input; report only that parsing failed.
		return nil, errors.New("Docker config is not valid JSON")
	}
	return config.Auths, nil
}

func (e dockerEntry) login() (authConfig, error) {
	if e.Username != "" && e.Password != "" {
		return authConfig{Username: e.Username, Password: e.Password}, nil
	}
	decoded, err := base64.StdEncoding.DecodeString(e.Auth)
	if err != nil {
		return authConfig{}, errors.New("auth is not base64")
	}
	user, pass, ok := strings.Cut(string(decoded), ":")
	if !ok || user == "" || pass == "" {
		return authConfig{}, errors.New("auth is not user:password")
	}
	return authConfig{Username: user, Password: pass}, nil
}

// install copies this executable to dest with mode 0755, through a temporary
// file in the same directory so a reader never sees a partial binary.
func install(dest string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	src, err := os.Open(self)
	if err != nil {
		return err
	}
	defer src.Close()
	tmp, err := os.CreateTemp(filepath.Dir(dest), ".credential-provider-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := io.Copy(tmp, src); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o755); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), dest)
}
