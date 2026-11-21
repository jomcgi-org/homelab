package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const secret = "ghp_not-a-real-token"

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), ".dockerconfigjson")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func ask(t *testing.T, image, config string) (response, error) {
	t.Helper()
	in := `{"apiVersion":"credentialprovider.kubelet.k8s.io/v1","kind":"CredentialProviderRequest","image":"` + image + `"}`
	var out bytes.Buffer
	err := provide(strings.NewReader(in), &out, config)
	var resp response
	if err == nil {
		if jerr := json.Unmarshal(out.Bytes(), &resp); jerr != nil {
			t.Fatal(jerr)
		}
	}
	return resp, err
}

func TestAnswersWithTheRegistryLogin(t *testing.T) {
	auth := base64.StdEncoding.EncodeToString([]byte("jomcgi:" + secret))
	config := writeConfig(t, `{"auths":{"ghcr.io":{"auth":"`+auth+`"}}}`)
	resp, err := ask(t, "ghcr.io/jomcgi/homelab/x@sha256:abc", config)
	if err != nil {
		t.Fatal(err)
	}
	if resp.APIVersion != apiVersion || resp.Kind != "CredentialProviderResponse" || resp.CacheKeyType != "Registry" {
		t.Fatalf("bad envelope: %+v", resp)
	}
	if got := resp.Auth["ghcr.io"]; got != (authConfig{Username: "jomcgi", Password: secret}) {
		t.Fatalf("auth = %+v", got)
	}
}

func TestUsernamePasswordAndSchemeKeys(t *testing.T) {
	config := writeConfig(t, `{"auths":{"https://ghcr.io":{"username":"u","password":"p"}}}`)
	resp, err := ask(t, "ghcr.io/a/b:tag", config)
	if err != nil {
		t.Fatal(err)
	}
	if resp.Auth["ghcr.io"] != (authConfig{Username: "u", Password: "p"}) {
		t.Fatalf("auth = %+v", resp.Auth)
	}
}

func TestOtherRegistryStaysAnonymous(t *testing.T) {
	config := writeConfig(t, `{"auths":{"ghcr.io":{"username":"u","password":"p"}}}`)
	for _, image := range []string{"quay.io/a/b", "busybox", "library/busybox:1"} {
		resp, err := ask(t, image, config)
		if err != nil {
			t.Fatal(image, err)
		}
		if len(resp.Auth) != 0 {
			t.Fatalf("%s got %+v", image, resp.Auth)
		}
	}
}

func TestErrorsNeverQuoteTheCredential(t *testing.T) {
	bad := base64.StdEncoding.EncodeToString([]byte(secret)) // no user:password
	for name, body := range map[string]string{
		"not base64":  `{"auths":{"ghcr.io":{"auth":"` + secret + `!"}}}`,
		"no colon":    `{"auths":{"ghcr.io":{"auth":"` + bad + `"}}}`,
		"broken json": `{"auths":{"ghcr.io":{"auth":"` + secret,
	} {
		_, err := ask(t, "ghcr.io/a/b", writeConfig(t, body))
		if err == nil {
			t.Fatalf("%s: want an error", name)
		}
		if strings.Contains(err.Error(), secret) || strings.Contains(err.Error(), bad) {
			t.Fatalf("%s: error quotes the credential: %v", name, err)
		}
	}
}

func TestRejectsBadRequests(t *testing.T) {
	config := writeConfig(t, `{"auths":{}}`)
	for _, in := range []string{
		`{"apiVersion":"credentialprovider.kubelet.k8s.io/v1beta1","kind":"CredentialProviderRequest","image":"ghcr.io/a"}`,
		`{"apiVersion":"credentialprovider.kubelet.k8s.io/v1","kind":"CredentialProviderRequest","image":""}`,
		`not json`,
	} {
		if err := provide(strings.NewReader(in), &bytes.Buffer{}, config); err == nil {
			t.Fatalf("want an error for %s", in)
		}
	}
	if _, err := ask(t, "ghcr.io/a", ""); err == nil {
		t.Fatal("want an error without DOCKER_CONFIG_FILE")
	}
}

func TestInstallCopiesAnExecutable(t *testing.T) {
	dest := filepath.Join(t.TempDir(), "credential-provider")
	if err := install(dest); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(dest)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o755 || info.Size() == 0 {
		t.Fatalf("mode %v size %d", info.Mode().Perm(), info.Size())
	}
}
