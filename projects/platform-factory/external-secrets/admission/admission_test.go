package admission_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/dynamic"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

const prefix = "factory-credentials-"

type admissionCase struct {
	object  *unstructured.Unstructured
	policy  string
	message string
}

// These expectations are independent of the rendered CEL and its messages.
// A schema, RBAC, webhook or CEL evaluation error must not pass a denial test.
var deniedBy = map[string][2]string{
	"home-70-denied-pod":               {"no-pods", "Pods may not run in factory-credentials."},
	"home-70-denied-no-repositories":   {"github-scope", "GithubAccessToken requires non-empty repositories."},
	"home-70-denied-no-permissions":    {"github-scope", "GithubAccessToken requires non-empty permissions."},
	"home-70-denied-outside-namespace": {"generators", "Credential generators belong in factory-credentials."},
	"home-70-denied-webhook":           {"generators", "Credential generators belong in factory-credentials."},
	"home-70-denied-cluster-generator": {"no-cluster-or-push", "Cluster generators, cluster stores, cluster external secrets and push secrets are disabled."},
	"home-70-denied-cluster-store":     {"no-cluster-or-push", "Cluster generators, cluster stores, cluster external secrets and push secrets are disabled."},
	"home-70-denied-cluster-external":  {"no-cluster-or-push", "Cluster generators, cluster stores, cluster external secrets and push secrets are disabled."},
	"home-70-denied-push":              {"no-cluster-or-push", "Cluster generators, cluster stores, cluster external secrets and push secrets are disabled."},
	"home-70-denied-cluster-push":      {"no-cluster-or-push", "Cluster generators, cluster stores, cluster external secrets and push secrets are disabled."},
	"home-70-denied-list":              {"secret-readers", "Secret readers in factory-credentials require resourceNames and only get."},
	"home-70-denied-unnamed-read":      {"secret-readers", "Secret readers in factory-credentials require resourceNames and only get."},
}

func inputPath(t *testing.T, key string) string {
	t.Helper()
	path := os.Getenv(key)
	if path == "" {
		t.Fatalf("missing %s; run the admission_test Bazel target", key)
	}
	if !filepath.IsAbs(path) && os.Getenv("TEST_SRCDIR") != "" {
		path = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), path)
	}
	return path
}

func documents(t *testing.T, data []byte) []*unstructured.Unstructured {
	t.Helper()
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(data), 4096)
	var docs []*unstructured.Unstructured
	for {
		var object map[string]interface{}
		err := decoder.Decode(&object)
		if err == io.EOF {
			return docs
		}
		if err != nil {
			t.Fatal(err)
		}
		if len(object) != 0 {
			docs = append(docs, &unstructured.Unstructured{Object: object})
		}
	}
}

func readDocuments(t *testing.T, key string) []*unstructured.Unstructured {
	t.Helper()
	data, err := os.ReadFile(inputPath(t, key))
	if err != nil {
		t.Fatal(err)
	}
	return documents(t, data)
}

func render(t *testing.T, chartKey string, extra ...string) []*unstructured.Unstructured {
	t.Helper()
	args := []string{"template", "external-secrets", filepath.Dir(inputPath(t, chartKey)), "--namespace", "external-secrets", "--include-crds"}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, inputPath(t, "HELM_BIN"), append(args, extra...)...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	output, err := cmd.Output()
	if err != nil {
		t.Fatalf("helm template: %v\n%s", err, stderr.String())
	}
	return documents(t, output)
}

func resource(client dynamic.Interface, object *unstructured.Unstructured) dynamic.ResourceInterface {
	plural := map[string]string{
		"Namespace": "namespaces", "Pod": "pods", "Role": "roles",
		"GithubAccessToken": "githubaccesstokens", "Webhook": "webhooks",
		"ClusterGenerator": "clustergenerators", "ClusterSecretStore": "clustersecretstores",
		"ClusterExternalSecret": "clusterexternalsecrets", "PushSecret": "pushsecrets",
		"ClusterPushSecret": "clusterpushsecrets", "ValidatingAdmissionPolicy": "validatingadmissionpolicies",
		"ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
	}[object.GetKind()]
	if plural == "" {
		panic("unmapped resource kind: " + object.GetKind())
	}
	gv := object.GroupVersionKind().GroupVersion()
	r := client.Resource(schema.GroupVersionResource{Group: gv.Group, Version: gv.Version, Resource: plural})
	if object.GetNamespace() != "" {
		return r.Namespace(object.GetNamespace())
	}
	return r
}

func expectedDenial(err error, tc admissionCase) bool {
	return err != nil && (apierrors.IsForbidden(err) || apierrors.IsInvalid(err)) &&
		strings.Contains(err.Error(), prefix+tc.policy) &&
		strings.Contains(err.Error(), "denied request") && strings.Contains(err.Error(), tc.message)
}

func TestCredentialAdmission(t *testing.T) {
	// A pin mismatch must fail before testing a different schema from deployment.
	chart := readDocuments(t, "ESO_UPSTREAM")[0]
	application := readDocuments(t, "ESO_APPLICATION")[0]
	sources, _, err := unstructured.NestedSlice(application.Object, "spec", "sources")
	if err != nil {
		t.Fatal(err)
	}
	matched := false
	for _, raw := range sources {
		source := raw.(map[string]interface{})
		if source["chart"] == "external-secrets" {
			matched = source["targetRevision"] == chart.Object["version"]
		}
	}
	if !matched {
		t.Fatal("the pinned test chart must match the Application targetRevision")
	}

	// Install only the seven CRDs exercised here, with their released schemas.
	// There is no ESO controller, kubelet or credential material in this server.
	wanted := map[string]bool{
		"GithubAccessToken": true, "Webhook": true, "ClusterGenerator": true,
		"ClusterSecretStore": true, "ClusterExternalSecret": true,
		"PushSecret": true, "ClusterPushSecret": true,
	}
	crdDir := t.TempDir()
	for _, doc := range render(t, "ESO_UPSTREAM", "-f", inputPath(t, "ESO_VALUES")) {
		if doc.GetKind() != "CustomResourceDefinition" {
			continue
		}
		kind, _, err := unstructured.NestedString(doc.Object, "spec", "names", "kind")
		if err != nil {
			t.Fatal(err)
		}
		if !wanted[kind] {
			continue
		}
		data, err := json.Marshal(doc.Object)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(crdDir, doc.GetName()+".json"), data, 0o600); err != nil {
			t.Fatal(err)
		}
		delete(wanted, kind)
	}
	if len(wanted) != 0 {
		t.Fatalf("missing released CRDs: %v", wanted)
	}

	assets := filepath.Dir(inputPath(t, "TEST_APISERVER"))
	t.Setenv("KUBEBUILDER_ASSETS", assets)
	for _, binary := range []string{"kube-apiserver", "etcd", "kubectl"} {
		// Override per-binary envtest paths too; never use an ambient cluster.
		t.Setenv("TEST_ASSET_"+strings.ToUpper(strings.ReplaceAll(binary, "-", "_")), filepath.Join(assets, binary))
	}
	useExisting := false
	environment := &envtest.Environment{
		UseExistingCluster:       &useExisting,
		BinaryAssetsDirectory:    assets,
		CRDDirectoryPaths:        []string{crdDir},
		CRDInstallOptions:        envtest.CRDInstallOptions{MaxTime: time.Minute},
		ErrorIfCRDPathMissing:    true,
		ControlPlaneStartTimeout: time.Minute,
		ControlPlaneStopTimeout:  30 * time.Second,
	}
	// Bazel runs this test without network access, so the sandbox has only
	// loopback and no default route. Without an explicit advertise address,
	// kube-apiserver derives one from the default route and exits at startup.
	// A loopback address is valid only with no endpoint reconciler. The test
	// calls the API server directly and never uses the kubernetes Service.
	environment.ControlPlane.GetAPIServer().Configure().
		Set("advertise-address", "127.0.0.1").
		Set("endpoint-reconciler-type", "none")
	t.Cleanup(func() {
		if err := environment.Stop(); err != nil {
			t.Errorf("stop private API server: %v", err)
		}
	})
	config, err := environment.Start()
	if err != nil {
		t.Fatalf("start private API server: %v", err)
	}
	client, err := dynamic.NewForConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	guards := render(t, "ESO_GUARDS")
	for _, doc := range guards {
		if doc.GetKind() == "Namespace" {
			if _, err := resource(client, doc).Create(ctx, doc, metav1.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
		}
	}
	cases := fixtureCases(t)
	baselines := make(map[string]*unstructured.Unstructured)
	for _, tc := range cases {
		// Prove each request is otherwise admissible before enabling the guards.
		if _, err := resource(client, tc.object).Create(ctx, tc.object, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}, FieldValidation: metav1.FieldValidationStrict}); err != nil {
			t.Fatalf("fixture %s fails without policies: %v", tc.object.GetName(), err)
		}
		baseline := tc.object.DeepCopy()
		baseline.SetName(baseline.GetName() + "-update")
		// Updates to Roles and GitHub generators attempt to widen valid grants.
		if baseline.GetKind() == "Role" {
			baseline.Object["rules"] = []interface{}{map[string]interface{}{
				"apiGroups": []interface{}{""}, "resources": []interface{}{"configmaps"}, "verbs": []interface{}{"get"},
			}}
		}
		if baseline.GetKind() == "GithubAccessToken" {
			spec := baseline.Object["spec"].(map[string]interface{})
			spec["repositories"] = []interface{}{"homelab"}
			spec["permissions"] = map[string]interface{}{"contents": "read"}
		}
		stored, err := resource(client, baseline).Create(ctx, baseline, metav1.CreateOptions{})
		if err != nil {
			t.Fatalf("seed update fixture %s: %v", baseline.GetName(), err)
		}
		baselines[tc.object.GetName()] = stored
	}
	for _, doc := range guards {
		if doc.GetKind() == "ValidatingAdmissionPolicy" || doc.GetKind() == "ValidatingAdmissionPolicyBinding" {
			if _, err := resource(client, doc).Create(ctx, doc, metav1.CreateOptions{}); err != nil {
				t.Fatalf("install %s %s: %v", doc.GetKind(), doc.GetName(), err)
			}
		}
	}
	// Admission watches are asynchronous. Require an actual rejection by every
	// named policy before exercising the complete CREATE and UPDATE matrix.
	ready := make(map[string]bool)
	for _, tc := range cases {
		if tc.policy == "" || ready[tc.policy] {
			continue
		}
		deadline := time.Now().Add(30 * time.Second)
		for {
			_, err := resource(client, tc.object).Create(ctx, tc.object, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
			if expectedDenial(err, tc) {
				ready[tc.policy] = true
				break
			}
			if err != nil || time.Now().After(deadline) {
				t.Fatalf("policy %s did not become effective: %v", tc.policy, err)
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
	if len(ready) != 5 {
		t.Fatalf("expected behavior from all five policies, got %v", ready)
	}
	for _, tc := range cases {
		t.Run(tc.object.GetName(), func(t *testing.T) {
			for _, operation := range []string{"CREATE", "UPDATE"} {
				t.Run(operation, func(t *testing.T) {
					var err error
					if operation == "CREATE" {
						_, err = resource(client, tc.object).Create(ctx, tc.object, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
					} else {
						update := baselines[tc.object.GetName()].DeepCopy()
						// Keep Pod defaults and immutable fields from the server.
						if update.GetKind() != "Pod" {
							for _, field := range []string{"spec", "rules"} {
								if value, ok := tc.object.DeepCopy().Object[field]; ok {
									update.Object[field] = value
								}
							}
						}
						update.SetAnnotations(map[string]string{"home-70-test": "update"})
						_, err = resource(client, update).Update(ctx, update, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}})
					}
					if tc.policy == "" {
						if err != nil {
							t.Fatalf("positive control rejected: %v", err)
						}
					} else if !expectedDenial(err, tc) {
						t.Fatalf("want %s denial %q; got %v", prefix+tc.policy, tc.message, err)
					}
				})
			}
		})
	}
}

func fixtureCases(t *testing.T) []admissionCase {
	t.Helper()
	var cases []admissionCase
	allowed := readDocuments(t, "ESO_ALLOWED")
	if len(allowed) != 5 {
		t.Fatal("expected all five positive controls")
	}
	for _, object := range allowed {
		cases = append(cases, admissionCase{object: object})
	}
	denied := readDocuments(t, "ESO_DENIED")
	if len(denied) != len(deniedBy) {
		t.Fatal("denial fixtures and policy expectations must agree")
	}
	for _, object := range denied {
		expect, ok := deniedBy[object.GetName()]
		if !ok {
			t.Fatalf("no policy expectation for %s", object.GetName())
		}
		cases = append(cases, admissionCase{object: object, policy: expect[0], message: expect[1]})
	}
	for _, object := range allowed {
		if object.GetKind() == "GithubAccessToken" {
			for _, field := range []string{"repositories", "permissions"} {
				var values []interface{}
				if field == "repositories" {
					values = []interface{}{[]interface{}{}, []interface{}{""}}
				} else {
					values = []interface{}{map[string]interface{}{}, map[string]interface{}{"contents": ""}, map[string]interface{}{"": "read"}}
				}
				for i, value := range values {
					variant := object.DeepCopy()
					variant.SetName(fmt.Sprintf("empty-%s-%d", field, i))
					variant.Object["spec"].(map[string]interface{})[field] = value
					cases = append(cases, admissionCase{variant, "github-scope", "GithubAccessToken requires non-empty " + field + "."})
				}
			}
		}
		if object.GetKind() == "Role" && object.GetNamespace() == "factory-credentials" {
			for _, verb := range []string{"list", "watch", "create", "update", "patch", "delete", "*"} {
				variant := object.DeepCopy()
				variant.SetName("secret-verb-" + strings.ReplaceAll(verb, "*", "all"))
				rule := variant.Object["rules"].([]interface{})[0].(map[string]interface{})
				rule["verbs"] = []interface{}{verb}
				cases = append(cases, admissionCase{variant, "secret-readers", deniedBy["home-70-denied-list"][1]})
			}
			for _, field := range []string{"resourceNames", "apiGroups", "resources"} {
				variant := object.DeepCopy()
				variant.SetName("secret-wildcard-" + strings.ToLower(field))
				rule := variant.Object["rules"].([]interface{})[0].(map[string]interface{})
				delete(rule, "resourceNames")
				rule[field] = []interface{}{"*"}
				if field == "resourceNames" {
					rule[field] = []interface{}{}
				}
				cases = append(cases, admissionCase{variant, "secret-readers", deniedBy["home-70-denied-list"][1]})
			}
		}
	}
	return cases
}
