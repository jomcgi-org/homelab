package runbookadmission_test

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/runbookadmission"

	registration "k8s.io/api/admissionregistration/v1"

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
	rendered, err := exec.Command(input(t, "HELM_BIN"), "template", "factory", filepath.Dir(input(t, "FACTORY_CHART")), "-s", "templates/runbook-revert.yaml", "-s", "templates/runbook-deadline.yaml", "--set", "runbookRevert.enabled=true", "-f", input(t, "DEPLOY_VALUES")).Output()
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
	server := httptest.NewTLSServer(runbookadmission.Handler{Journals: client.CoreV1().ConfigMaps("grafana")})
	defer server.Close()
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
		if obj.GetKind() == "ValidatingWebhookConfiguration" {
			var config registration.ValidatingWebhookConfiguration
			data, err := obj.MarshalJSON()
			if err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(data, &config); err != nil {
				t.Fatal(err)
			}
			for i := range config.Webhooks {
				config.Webhooks[i].ClientConfig = registration.WebhookClientConfig{URL: &server.URL, CABundle: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})}
			}
			if _, err = client.AdmissionregistrationV1().ValidatingWebhookConfigurations().Create(ctx, &config, meta.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
			continue
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
		{"system:serviceaccount:grafana:runbook-deadline", "get", "grafana", "configmaps", "", "runbook-revert-factory-connector", true},
		{"system:serviceaccount:grafana:runbook-deadline", "update", "grafana", "configmaps", "", "runbook-revert-factory-connector", false},
		{"system:serviceaccount:grafana:runbook-deadline", "delete", "factory-r-homelab", "pods", "", "worker", false},
		{"system:serviceaccount:grafana:runbooks", "delete", "factory-r-homelab", "pods", "", "worker", false},
		{"system:serviceaccount:grafana:runbooks", "update", "grafana", "configmaps", "", "runbook-revert-factory-connector", false},
		{"system:serviceaccount:factory:factory-reviewer", "update", "grafana", "configmaps", "", "runbook-revert-factory-connector", false},
		{"system:serviceaccount:factory:factory-connector", "update", "grafana", "configmaps", "", "runbook-revert-factory-connector", true},
		{"system:serviceaccount:factory:factory-connector", "get", "factory-r-homelab", "pods", "", "worker", true},
		{"system:serviceaccount:factory:factory-connector", "get", "factory-r-homelab-review", "pods", "", "worker", false},
		{"system:serviceaccount:factory:factory-reviewer", "get", "factory-r-homelab-review", "pods", "", "worker", true},
		{"system:serviceaccount:factory:factory-reviewer", "get", "factory-r-homelab", "pods", "", "worker", false},
		{"system:serviceaccount:factory:factory-connector", "delete", "factory-r-homelab", "pods", "", "worker", false},
		{"system:serviceaccount:factory:factory-reviewer", "delete", "factory-r-homelab-review", "pods", "", "worker", false},
		{"system:serviceaccount:factory:factory-connector", "update", "grafana", "configmaps", "", "runbook-revert-factory-reviewer", false},
		{"system:serviceaccount:factory:factory-reviewer", "update", "grafana", "configmaps", "", "runbook-revert-factory-reviewer", true},
	} {
		deadline := time.Now().Add(10 * time.Second)
		for {
			review, err := client.AuthorizationV1().SubjectAccessReviews().Create(ctx, &auth.SubjectAccessReview{Spec: auth.SubjectAccessReviewSpec{User: tc.user, Groups: []string{"system:authenticated", "system:serviceaccounts", "system:serviceaccounts:" + strings.Split(tc.user, ":")[2]}, ResourceAttributes: &auth.ResourceAttributes{Verb: tc.verb, Namespace: tc.ns, Resource: tc.resource, Subresource: tc.sub, Name: tc.name}}}, meta.CreateOptions{})
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

	userCfg := rest.CopyConfig(cfg)
	userCfg.Impersonate = rest.ImpersonationConfig{UserName: front, Groups: []string{"system:authenticated", "system:serviceaccounts", "system:serviceaccounts:grafana"}}
	user, err := kubernetes.NewForConfig(userCfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, pool := range []struct{ owner, namespace, prefix string }{
		{"factory-connector", "factory-r-homelab", "homelab-codex-"},
		{"factory-reviewer", "factory-r-homelab-review", "homelab-reviewer-"},
	} {
		t.Run(pool.owner, func(t *testing.T) { testWorkerScope(t, client, user, pool.owner, pool.namespace, pool.prefix) })
	}
}

func testWorkerScope(t *testing.T, client, user kubernetes.Interface, owner, ns, prefix string) {
	ctx := t.Context()
	_, err := client.CoreV1().ServiceAccounts(ns).Create(ctx, &v1.ServiceAccount{ObjectMeta: meta.ObjectMeta{Name: "default"}}, meta.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	journal, err := client.CoreV1().ConfigMaps("grafana").Create(ctx, &v1.ConfigMap{ObjectMeta: meta.ObjectMeta{Name: "runbook-revert-" + owner, Namespace: "grafana"}, Data: map[string]string{"phase": "confirmed"}}, meta.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"unrelated", prefix + "12345678-abcde"} {
		pod, err := client.CoreV1().Pods(ns).Create(ctx, &v1.Pod{ObjectMeta: meta.ObjectMeta{Name: name}, Spec: v1.PodSpec{Containers: []v1.Container{{Name: "worker", Image: "pause"}}}}, meta.CreateOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if name != "unrelated" {
			journal.Data = map[string]string{"deadline": time.Now().Add(time.Minute).Format(time.RFC3339Nano), "id": "recovery-1", "user": "joe", "session": "session-1", "executor": "executor-1", "creator": owner, "phase": "deleting", "namespace": ns, "pod": name, "uid": string(pod.UID)}
			if journal, err = client.CoreV1().ConfigMaps("grafana").Update(ctx, journal, meta.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
		}
		opts := meta.DeleteOptions{DryRun: []string{meta.DryRunAll}, Preconditions: &meta.Preconditions{UID: &pod.UID}}
		deadline := time.Now().Add(10 * time.Second)
		for {
			err = user.CoreV1().Pods(ns).Delete(ctx, name, opts)
			if name == "unrelated" && err != nil && (strings.Contains(err.Error(), "Only UID-fenced worker deletes") || strings.Contains(err.Error(), "worker authorization mismatch")) || name != "unrelated" && err == nil {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("delete %s: %v", name, err)
			}
			time.Sleep(100 * time.Millisecond)
		}
		if name != "unrelated" {
			// A second, correctly named worker in the same pool has the same
			// RBAC grant. The confirmed journal must confine the action to one UID.
			other, err := client.CoreV1().Pods(ns).Create(ctx, &v1.Pod{ObjectMeta: meta.ObjectMeta{Name: prefix + "87654321-abcde"}, Spec: pod.Spec}, meta.CreateOptions{})
			if err != nil {
				t.Fatal(err)
			}
			otherOpts := opts.DeepCopy()
			otherOpts.Preconditions.UID = &other.UID
			err = user.CoreV1().Pods(ns).Delete(ctx, other.Name, *otherOpts)
			if err == nil || !(strings.Contains(err.Error(), "Only UID-fenced worker deletes") || strings.Contains(err.Error(), "worker authorization mismatch")) {
				t.Fatalf("delete unconfirmed sibling: %v", err)
			}
			changed := journal.DeepCopy()
			changed.Data["pod"] = other.Name
			changed.Data["uid"] = string(other.UID)
			// The journal policy is the first one this user meets on a
			// ConfigMap update. Wait for the policy cache to load it.
			retarget := time.Now().Add(10 * time.Second)
			for {
				_, err = user.CoreV1().ConfigMaps("grafana").Update(ctx, changed, meta.UpdateOptions{DryRun: []string{meta.DryRunAll}})
				if err != nil && strings.Contains(err.Error(), "confirmed identity and targets are immutable") {
					break
				}
				if time.Now().After(retarget) {
					t.Fatalf("retarget confirmed operation: %v", err)
				}
				time.Sleep(100 * time.Millisecond)
			}
			// Keep the same pod UID and the same deleting phase. Expiry alone must deny it.
			journal.Data["deadline"] = time.Now().Add(-time.Second).Format(time.RFC3339Nano)
			if journal, err = client.CoreV1().ConfigMaps("grafana").Update(ctx, journal, meta.UpdateOptions{}); err != nil {
				t.Fatal(err)
			}
			until := time.Now().Add(10 * time.Second)
			for {
				err = user.CoreV1().Pods(ns).Delete(ctx, name, opts)
				if err != nil && strings.Contains(err.Error(), "worker authorization expired") {
					break
				}
				if time.Now().After(until) {
					t.Fatalf("expired deletion: %v", err)
				}
				time.Sleep(100 * time.Millisecond)
			}
			journal.Data["phase"] = "authorized"
			journal, err = client.CoreV1().ConfigMaps("grafana").Update(ctx, journal, meta.UpdateOptions{})
			if err != nil {
				t.Fatal(err)
			}
			journal.Data["phase"] = "deleting"
			_, err = user.CoreV1().ConfigMaps("grafana").Update(ctx, journal, meta.UpdateOptions{DryRun: []string{meta.DryRunAll}})
			if err == nil || !strings.Contains(err.Error(), "worker authorization expired") {
				t.Fatalf("expired journal transition: %v", err)
			}

		}

	}
}
