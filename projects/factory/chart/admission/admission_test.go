package runbookadmission_test

import (
	"bytes"
	"context"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	auth "k8s.io/api/authorization/v1"
	v1 "k8s.io/api/core/v1"
	meta "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

func input(t *testing.T, key string) string {
	t.Helper()
	path := os.Getenv(key)
	if path == "" {
		t.Fatalf("missing %s; run the Bazel admission_test target", key)
	}
	if !filepath.IsAbs(path) && os.Getenv("TEST_SRCDIR") != "" {
		path = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), path)
	}
	return path
}

// Exercise the API server's RBAC authorizer against the actual chart grants.
func TestActionPermissions(t *testing.T) {
	ctx := context.Background()
	rendered, err := exec.Command(input(t, "HELM_BIN"), "template", "factory", filepath.Dir(input(t, "FACTORY_CHART")), "-s", "templates/runbook-revert.yaml", "--set", "runbookRevert.enabled=true", "-f", input(t, "DEPLOY_VALUES")).Output()
	if err != nil {
		t.Fatal(err)
	}
	assets := filepath.Dir(input(t, "TEST_APISERVER"))
	for _, binary := range []string{"kube-apiserver", "etcd", "kubectl"} {
		t.Setenv("TEST_ASSET_"+strings.ToUpper(strings.ReplaceAll(binary, "-", "_")), filepath.Join(assets, binary))
	}
	existing := false
	env := &envtest.Environment{UseExistingCluster: &existing, BinaryAssetsDirectory: assets}
	env.ControlPlane.GetAPIServer().Configure().Set("authorization-mode", "RBAC").Set("advertise-address", "127.0.0.1").Set("endpoint-reconciler-type", "none")
	cfg, err := env.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := env.Stop(); err != nil {
			t.Error(err)
		}
	})
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	dyn, err := dynamic.NewForConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, ns := range []string{"factory", "grafana", "factory-r-homelab", "factory-r-homelab-review"} {
		_, err = client.CoreV1().Namespaces().Create(ctx, &v1.Namespace{ObjectMeta: meta.ObjectMeta{Name: ns}}, meta.CreateOptions{})
		if err != nil {
			t.Fatal(err)
		}
	}
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(rendered), 4096)
	for {
		var obj unstructured.Unstructured
		err := decoder.Decode(&obj)
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		plural := map[string]string{"Role": "roles", "RoleBinding": "rolebindings", "ValidatingAdmissionPolicy": "validatingadmissionpolicies", "ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings"}[obj.GetKind()]
		if plural == "" {
			continue
		}
		gv := obj.GroupVersionKind().GroupVersion()
		r := dyn.Resource(schema.GroupVersionResource{Group: gv.Group, Version: gv.Version, Resource: plural})
		var resource dynamic.ResourceInterface = r
		if obj.GetNamespace() != "" {
			resource = r.Namespace(obj.GetNamespace())
		}
		if _, err = resource.Create(ctx, &obj, meta.CreateOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	front := "system:serviceaccount:grafana:runbook-kagent-revert"
	for _, tc := range []struct {
		user, verb, ns, resource, sub, name string
		allowed                             bool
	}{
		{front, "delete", "factory-r-homelab", "pods", "", "worker", true},
		{front, "delete", "factory", "pods", "", "worker", false},
		{front, "create", "factory", "serviceaccounts", "token", "factory-reviewer", false},
		{front, "get", "grafana", "secrets", "", "runbook-discord", false},
		{front, "update", "grafana", "configmaps", "", "unrelated", false},
		{"system:serviceaccount:factory:factory-connector", "update", "grafana", "configmaps", "", "runbook-revert-factory-reviewer", false},
		{"system:serviceaccount:factory:factory-reviewer", "update", "grafana", "configmaps", "", "runbook-revert-factory-reviewer", true},
	} {
		deadline := time.Now().Add(10 * time.Second)
		for {
			review, err := client.AuthorizationV1().SubjectAccessReviews().Create(ctx, &auth.SubjectAccessReview{Spec: auth.SubjectAccessReviewSpec{User: tc.user, ResourceAttributes: &auth.ResourceAttributes{Verb: tc.verb, Namespace: tc.ns, Resource: tc.resource, Subresource: tc.sub, Name: tc.name}}}, meta.CreateOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if review.Status.Allowed == tc.allowed {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("permission mismatch for %+v", tc)
			}
			time.Sleep(100 * time.Millisecond)
		}
	}

	// RBAC scopes deletion to two namespaces; admission narrows it to the
	// reviewed worker name and UID within them. Wait for policy cache propagation.
	ns := "factory-r-homelab"
	_, err = client.CoreV1().ServiceAccounts(ns).Create(ctx, &v1.ServiceAccount{ObjectMeta: meta.ObjectMeta{Name: "default"}}, meta.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	userCfg := rest.CopyConfig(cfg)
	userCfg.Impersonate = rest.ImpersonationConfig{UserName: front, Groups: []string{"system:authenticated"}}
	user, err := kubernetes.NewForConfig(userCfg)
	if err != nil {
		t.Fatal(err)
	}
	journal, err := client.CoreV1().ConfigMaps("grafana").Create(ctx, &v1.ConfigMap{ObjectMeta: meta.ObjectMeta{Name: "runbook-revert-factory-connector", Namespace: "grafana"}, Data: map[string]string{"phase": "confirmed"}}, meta.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"unrelated", "homelab-codex-12345678-abcde"} {
		pod, err := client.CoreV1().Pods(ns).Create(ctx, &v1.Pod{ObjectMeta: meta.ObjectMeta{Name: name}, Spec: v1.PodSpec{Containers: []v1.Container{{Name: "worker", Image: "pause"}}}}, meta.CreateOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if name != "unrelated" {
			journal.Data = map[string]string{"phase": "deleting", "namespace": ns, "pod": name, "uid": string(pod.UID)}
			if _, err = client.CoreV1().ConfigMaps("grafana").Update(ctx, journal, meta.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
		}
		opts := meta.DeleteOptions{DryRun: []string{meta.DryRunAll}, Preconditions: &meta.Preconditions{UID: &pod.UID}}
		deadline := time.Now().Add(10 * time.Second)
		for {
			err = user.CoreV1().Pods(ns).Delete(ctx, name, opts)
			if name == "unrelated" && err != nil && strings.Contains(err.Error(), "Only UID-fenced worker deletes") || name != "unrelated" && err == nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("delete %s: %v", name, err)
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
}
