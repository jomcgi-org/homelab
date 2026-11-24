package detecttest

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/url"
	"strings"
)

// Sample is one fake input. A secret sample holds a made-up credential of a
// known shape. Nothing here is a real credential, and nothing is written as a
// literal: the values are built at run time from fragments and a fixed seed,
// so the repository holds no token-shaped text.
type Sample struct {
	Name   string
	Text   string
	Secret bool
	// Encoded means the credential is only visible after decoding (base64, hex,
	// percent encoding, gzip). A detector without a decoder misses these.
	Encoded bool
}

const alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

type gen struct{ r *rand.Rand }

func (g gen) str(n int, alphabet string) string {
	var b strings.Builder
	for range n {
		b.WriteByte(alphabet[g.r.Intn(len(alphabet))])
	}
	return b.String()
}

func (g gen) hex(n int) string { return g.str(n, "0123456789abcdef") }

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

// Corpus returns the fake samples. The same seed gives the same samples.
func Corpus(seed int64) []Sample {
	g := gen{rand.New(rand.NewSource(seed))}
	gh := "gh" + "p_" + g.str(36, alnum)
	awsID := "AK" + "IA" + g.str(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
	awsKey := g.str(40, alnum+"/+")
	slack := "xo" + "xb-" + g.str(11, "0123456789") + "-" + g.str(12, "0123456789") + "-" + g.str(24, alnum)
	stripe := "sk" + "_live_" + g.str(24, alnum)
	pemBody := new(strings.Builder)
	for range 26 {
		pemBody.WriteString(b64(g.str(48, alnum)) + "\n")
	}
	pem := "-----BEGIN " + "RSA PRIVATE KEY-----\n" + pemBody.String() + "-----END " + "RSA PRIVATE KEY-----"
	jwtPart := func(v any) string {
		raw, _ := json.Marshal(v)
		return strings.TrimRight(base64.URLEncoding.EncodeToString(raw), "=")
	}
	jwt := jwtPart(map[string]string{"alg": "HS256", "typ": "JWT"}) + "." +
		jwtPart(map[string]any{"sub": "1234567890", "name": "Fake User", "iat": 1716239022, "exp": 1916239022}) + "." +
		strings.TrimRight(base64.URLEncoding.EncodeToString([]byte(g.str(32, alnum))), "=")
	bearer := g.str(40, alnum)
	var gz bytes.Buffer
	zw := gzip.NewWriter(&gz)
	_, _ = zw.Write([]byte("token=" + gh))
	_ = zw.Close()
	k8sSecret := func(key, value string) string {
		return "apiVersion: v1\nkind: Secret\nmetadata:\n  name: app\ndata:\n  " + key + ": " + b64(value) + "\n"
	}
	jsonOf := func(v any) string { raw, _ := json.Marshal(v); return string(raw) }

	secrets := []Sample{
		{Name: "plain-github", Text: "note: use token " + gh + " for the deploy"},
		{Name: "plain-aws", Text: "aws_access_key_id = " + awsID + "\naws_secret_access_key = " + awsKey},
		{Name: "plain-slack", Text: "slack bot token " + slack + " posted to the channel"},
		{Name: "plain-stripe", Text: "STRIPE_SECRET=" + stripe},
		{Name: "plain-gcp-key", Text: "maps key=AI" + "zaSy" + g.str(33, alnum+"-_")},
		{Name: "plain-sendgrid", Text: "SENDGRID_API_KEY=SG." + g.str(22, alnum) + "." + g.str(43, alnum)},
		{Name: "plain-npm", Text: "//registry.npmjs.org/:_authToken=npm_" + g.str(36, alnum)},
		{Name: "plain-anthropic", Text: "ANTHROPIC_API_KEY=sk-" + "ant-api03-" + g.str(93, alnum+"-_") + "AA"},
		{Name: "json-github", Text: jsonOf(map[string]string{"lesson": "deploy", "token": gh})},
		{Name: "json-unicode-escape", Text: `{"t":"` + strings.ReplaceAll(gh, "_", "\\u005f") + `"}`},
		{Name: "json-code-in-string", Text: jsonOf(map[string]string{"content": "const c = new Octokit({ auth: \"" + gh + "\" });\nawait c.run();\n"})},
		{Name: "pem-private-key", Text: "key:\n" + pem + "\n"},
		{Name: "jwt", Text: "cookie: session=" + jwt},
		{Name: "bearer-40", Text: "Authorization: Bearer " + bearer},
		{Name: "basic-auth", Text: "Authorization: Basic " + b64("admin:"+g.str(18, alnum))},
		{Name: "database-url", Text: "DATABASE_URL=postgres://app:" + g.str(20, alnum) + "@db.internal.example.com:5432/app"},
		{Name: "env-dump", Text: "HOME=/home/user\nPATH=/usr/bin\nGITHUB_TOKEN=" + gh + "\nLANG=C.UTF-8\n"},
		{Name: "kubeconfig-token", Text: "users:\n- name: ci\n  user:\n    token: " + strings.ToLower(g.str(32, alnum)) + "." + strings.ToLower(g.str(16, alnum)) + "\n"},
		{Name: "k8s-secret-password", Text: k8sSecret("password", g.str(24, alnum))},
		{Name: "docker-config-auth", Text: jsonOf(map[string]any{"auths": map[string]any{"ghcr.io": map[string]string{"auth": b64("octo:" + gh)}}})},
		{Name: "slack-webhook", Text: "https://hooks.slack.com/services/T0" + g.str(8, alnum) + "/B0" + g.str(8, alnum) + "/" + g.str(24, alnum)},
		{Name: "base64-github", Text: b64("token=" + gh), Encoded: true},
		{Name: "base64-github-in-json", Text: jsonOf(map[string]string{"payload": b64("auth: " + gh)}), Encoded: true},
		{Name: "base64-aws-pair", Text: b64("AWS_ACCESS_KEY_ID=" + awsID + "\nAWS_SECRET_ACCESS_KEY=" + awsKey), Encoded: true},
		{Name: "base64-twice-github", Text: b64(b64("token=" + gh)), Encoded: true},
		{Name: "base64-pem", Text: "key=" + b64(pem), Encoded: true},
		{Name: "hex-github", Text: "blob=" + hex.EncodeToString([]byte(gh)), Encoded: true},
		{Name: "percent-encoded-key", Text: "GITHUB_TOKEN%3D" + strings.ReplaceAll(gh, "_", "%5F"), Encoded: true},
		{Name: "percent-encoded-query", Text: "q=" + url.QueryEscape("token="+gh), Encoded: true},
		{Name: "k8s-secret-github", Text: k8sSecret("token", gh), Encoded: true},
	}
	for i := range secrets {
		secrets[i].Secret = true
	}
	// Known gaps of every detector tested so far: a credential split over two
	// lines, and a gzip body inside base64. They are listed so that a detector
	// that catches one is a visible change.
	gaps := []Sample{
		{Name: "gap-split-over-lines", Text: "token " + gh[:20] + "\n" + gh[20:], Secret: true},
		{Name: "gap-gzip-base64", Text: "blob=" + base64.StdEncoding.EncodeToString(gz.Bytes()), Secret: true, Encoded: true},
	}
	clean := []Sample{
		{Name: "clean-prose", Text: "Remember to book the dentist on Tuesday, and ask Sam about the garden plan. The meeting notes mention three action items."},
		{Name: "clean-go-code", Text: "func (s *Server) Handle(ctx context.Context, req *Request) (*Response, error) {\n\tif req.ID == \"\" {\n\t\treturn nil, errors.New(\"missing id\")\n\t}\n\treturn &Response{OK: true}, nil\n}\n"},
		{Name: "clean-sha256", Text: "image sha256:" + g.hex(64) + " built from commit " + g.hex(40)},
		{Name: "clean-uuids", Text: fmt.Sprintf("ids: %s-0000-4000-8000-%s, %s-0000-4000-8000-%s", g.hex(8), g.hex(12), g.hex(8), g.hex(12))},
		{Name: "clean-random-base64", Text: "icon=" + b64(g.str(300, alnum))},
		{Name: "clean-json-memory", Text: jsonOf(map[string]any{"bank_id": "homelab", "content": "Lesson: when a pod is Pending, check node taints first.", "tags": []string{"lesson", "k8s"}})},
		{Name: "clean-lockfile-integrity", Text: `"integrity": "sha512-` + b64(g.str(64, alnum)) + `",`},
		{Name: "clean-hex-ids", Text: "trace_id=" + g.hex(32) + " span_id=" + g.hex(16)},
		{Name: "clean-docs-token-word", Text: "The API uses a bearer token. Pass the token in the Authorization header; never log the token or the secret key."},
		{Name: "clean-placeholders", Text: "export GITHUB_TOKEN=<your-token-here>\nexport AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}\n"},
		{Name: "clean-bearer-placeholder", Text: "Authorization: Bearer $TOKEN\nauthorization: Bearer -\n"},
		{Name: "clean-database-placeholder", Text: "DATABASE_URL=postgres://app:${DB_PASSWORD}@db.internal.example.com:5432/app"},
		{Name: "clean-logs", Text: "2026-11-24T09:00:01Z INFO pod egress-scan-6674db8df9-7mwvd ready in 3.2s node=node-2 reqs=14 p95=142ms\n"},
		{Name: "clean-base64-text", Text: "note=" + b64("remember to renew the domain before december and ask about the invoice")},
		{Name: "clean-git-diff", Text: "diff --git a/main.go b/main.go\nindex 83db48f..bf2a3c1 100644\n--- a/main.go\n+++ b/main.go\n@@ -10,3 +10,4 @@ func main() {\n+\tlog.Println(\"starting\")\n"},
		{Name: "clean-helm-values", Text: "image:\n  repository: ghcr.io/example/app\n  digest: sha256:" + g.hex(64) + "\nreplicas: 2\nsecretName: app-credentials\n"},
	}
	return append(append(secrets, gaps...), clean...)
}
