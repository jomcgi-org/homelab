package admission_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

const (
	namespace        = "factory-public"
	mirror           = "factory-public-factory-public-mirror"
	secret           = mirror + "-r2"
	message          = "Only the mirror controller chain may use its R2 Secret or ServiceAccount."
	attacker         = "factory-pod-creator"
	jobController    = "system:serviceaccount:kube-system:job-controller"
	cronController   = "system:serviceaccount:kube-system:cronjob-controller"
	argoController   = "system:serviceaccount:argocd:argocd-application-controller"
	garbageCollector = "system:serviceaccount:kube-system:generic-garbage-collector"
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

func pod(name, ns, account, reference string) *unstructured.Unstructured {
	container := map[string]interface{}{"name": "app", "image": "registry.k8s.io/pause:3.10"}
	spec := map[string]interface{}{
		"serviceAccountName": account, "automountServiceAccountToken": false,
		"containers": []interface{}{container},
	}
	switch reference {
	case "env", "init-env", "ephemeral-env":
		container["env"] = []interface{}{map[string]interface{}{
			"name": "TOKEN", "valueFrom": map[string]interface{}{
				"secretKeyRef": map[string]interface{}{"name": secret, "key": "access-key-id"},
			},
		}}
	case "envFrom", "init-envFrom", "ephemeral-envFrom":
		container["envFrom"] = []interface{}{map[string]interface{}{
			"secretRef": map[string]interface{}{"name": secret},
		}}
	case "volume":
		spec["volumes"] = []interface{}{map[string]interface{}{
			"name": "token", "secret": map[string]interface{}{"secretName": secret},
		}}
	case "projected":
		spec["volumes"] = []interface{}{map[string]interface{}{
			"name": "token", "projected": map[string]interface{}{
				"sources": []interface{}{map[string]interface{}{
					"secret": map[string]interface{}{"name": secret},
				}},
			},
		}}
	case "pull":
		spec["imagePullSecrets"] = []interface{}{map[string]interface{}{"name": secret}}
	}
	if strings.HasPrefix(reference, "init-") || strings.HasPrefix(reference, "ephemeral-") {
		field := "initContainers"
		if strings.HasPrefix(reference, "ephemeral-") {
			field = "ephemeralContainers"
		}
		container["name"] = "helper"
		spec[field] = []interface{}{container}
		spec["containers"] = []interface{}{map[string]interface{}{
			"name": "app", "image": "registry.k8s.io/pause:3.10",
		}}
	}
	return &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": "v1", "kind": "Pod",
		"metadata": map[string]interface{}{"name": name, "namespace": ns}, "spec": spec,
	}}
}

func denial(err error) bool {
	return policyDenial(err, namespace+"-"+secret, message)
}

func policyDenial(err error, policy, message string) bool {
	return err != nil && (apierrors.IsForbidden(err) || apierrors.IsInvalid(err)) &&
		strings.Contains(err.Error(), policy) &&
		strings.Contains(err.Error(), "denied request") && strings.Contains(err.Error(), message)
}

func readObjects(t *testing.T, key string) []*unstructured.Unstructured {
	t.Helper()
	data, err := os.ReadFile(input(t, key))
	if err != nil {
		t.Fatal(err)
	}
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(data), 4096)
	var result []*unstructured.Unstructured
	for {
		var o unstructured.Unstructured
		if err := decoder.Decode(&o); err == io.EOF {
			return result
		} else if err != nil {
			t.Fatal(err)
		}
		if o.GetKind() != "" {
			result = append(result, &o)
		}
	}
}

func resource(client dynamic.Interface, object *unstructured.Unstructured) dynamic.ResourceInterface {
	plural := map[string]string{
		"Pod": "pods", "Job": "jobs", "CronJob": "cronjobs",
		"Namespace": "namespaces", "ServiceAccount": "serviceaccounts", "Secret": "secrets",
		"ConfigMap": "configmaps", "Role": "roles", "RoleBinding": "rolebindings",
		"ValidatingAdmissionPolicy":        "validatingadmissionpolicies",
		"ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
	}[object.GetKind()]
	if plural == "" {
		panic("unmapped kind " + object.GetKind())
	}
	gv := object.GroupVersionKind().GroupVersion()
	r := client.Resource(schema.GroupVersionResource{Group: gv.Group, Version: gv.Version, Resource: plural})
	if object.GetNamespace() != "" {
		return r.Namespace(object.GetNamespace())
	}
	return r
}

func object(kind, name, ns string, extra map[string]interface{}) *unstructured.Unstructured {
	group := "v1"
	if kind == "Role" || kind == "RoleBinding" {
		group = "rbac.authorization.k8s.io/v1"
	}
	o := &unstructured.Unstructured{Object: map[string]interface{}{
		"apiVersion": group, "kind": kind, "metadata": map[string]interface{}{"name": name},
	}}
	if ns != "" {
		o.SetNamespace(ns)
	}
	for k, v := range extra {
		o.Object[k] = v
	}
	return o
}

// These references are deliberately forgeable. Only the authenticated caller
// can distinguish a controller request from a pod/job creator copying metadata.
func owned(o *unstructured.Unstructured, kind, name string) *unstructured.Unstructured {
	o.Object["metadata"].(map[string]interface{})["ownerReferences"] = []interface{}{map[string]interface{}{
		"apiVersion": "batch/v1", "kind": kind, "name": name,
		"uid": "00000000-0000-4000-8000-000000000001", "controller": true,
	}}
	return o
}

func batch(kind, name, account, reference string) *unstructured.Unstructured {
	spec := pod("template", namespace, account, reference).Object["spec"].(map[string]interface{})
	spec["restartPolicy"] = "Never"
	jobSpec := map[string]interface{}{"template": map[string]interface{}{"spec": spec}}
	if kind == "CronJob" {
		jobSpec = map[string]interface{}{
			"schedule": "* * * * *", "suspend": true,
			"jobTemplate": map[string]interface{}{"spec": jobSpec},
		}
	}
	o := object(kind, name, namespace, map[string]interface{}{"spec": jobSpec})
	o.SetAPIVersion("batch/v1")
	if kind == "Job" {
		owned(o, "CronJob", mirror)
	}
	return o
}

func TestMirrorSecretAdmission(t *testing.T) {
	assets := filepath.Dir(input(t, "TEST_APISERVER"))
	t.Setenv("KUBEBUILDER_ASSETS", assets)
	for _, binary := range []string{"kube-apiserver", "etcd", "kubectl"} {
		t.Setenv("TEST_ASSET_"+strings.ToUpper(strings.ReplaceAll(binary, "-", "_")), filepath.Join(assets, binary))
	}
	useExisting := false
	environment := &envtest.Environment{
		UseExistingCluster: &useExisting, BinaryAssetsDirectory: assets,
		ControlPlaneStartTimeout: time.Minute, ControlPlaneStopTimeout: 30 * time.Second,
	}
	environment.ControlPlane.GetAPIServer().Configure().
		Set("advertise-address", "127.0.0.1").Set("endpoint-reconciler-type", "none")
	t.Cleanup(func() {
		if err := environment.Stop(); err != nil {
			t.Error(err)
		}
	})
	config, err := environment.Start()
	if err != nil {
		t.Fatal(err)
	}
	client, err := dynamic.NewForConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	create := func(o *unstructured.Unstructured) *unstructured.Unstructured {
		stored, err := resource(client, o).Create(ctx, o, metav1.CreateOptions{})
		if err != nil {
			t.Fatalf("baseline %s/%s: %v", o.GetKind(), o.GetName(), err)
		}
		return stored
	}
	actors := []string{attacker, jobController, cronController, argoController, garbageCollector}
	clients := map[string]dynamic.Interface{}
	for _, user := range append(append([]string{}, actors...), "system:serviceaccount:garage:garage-init") {
		cfg := rest.CopyConfig(config)
		cfg.Impersonate = rest.ImpersonationConfig{UserName: user, Groups: []string{"system:authenticated"}}
		clients[user], err = dynamic.NewForConfig(cfg)
		if err != nil {
			t.Fatal(err)
		}
	}
	for _, ns := range []string{namespace, "other"} {
		create(object("Namespace", ns, "", nil))
		for _, account := range []string{"default", mirror} {
			create(object("ServiceAccount", account, ns, nil))
		}
		// Give actors create/update but no impersonation, Secrets or RBAC rights.
		create(object("Role", "test-creators", ns, map[string]interface{}{"rules": []interface{}{map[string]interface{}{
			"apiGroups": []interface{}{"", "batch"}, "resources": []interface{}{"pods", "pods/ephemeralcontainers", "jobs", "cronjobs", "configmaps"},
			"verbs": []interface{}{"create", "update", "delete"},
		}}}))
		subjects := []interface{}{}
		for _, user := range actors {
			subjects = append(subjects, map[string]interface{}{"kind": "User", "name": user, "apiGroup": "rbac.authorization.k8s.io"})
		}
		create(object("RoleBinding", "test-creators", ns, map[string]interface{}{
			"subjects": subjects, "roleRef": map[string]interface{}{"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "test-creators"},
		}))
	}
	create(object("Namespace", "garage", "", nil))
	create(object("ServiceAccount", "garage-init", "garage", nil))
	for _, o := range readObjects(t, "GARAGE_BOOTSTRAP") {
		if o.GetKind() == "Role" || o.GetKind() == "RoleBinding" {
			create(o)
		}
	}

	type testCase struct {
		object, stored     *unstructured.Unstructured
		creators, updaters []string
		subresource        string
	}
	var cases []testCase
	seed := func(o *unstructured.Unstructured, creators, updaters []string, subresource string) {
		baseline := o.DeepCopy()
		if subresource != "" {
			unstructured.RemoveNestedField(baseline.Object, "spec", "ephemeralContainers")
		}
		stored := create(baseline)
		if subresource != "" {
			containers, _, _ := unstructured.NestedSlice(o.Object, "spec", "ephemeralContainers")
			if err := unstructured.SetNestedSlice(stored.Object, containers, "spec", "ephemeralContainers"); err != nil {
				t.Fatal(err)
			}
			if _, err := resource(client, stored).Update(ctx, stored, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}}, subresource); err != nil {
				t.Fatal(err)
			}
		}
		cases = append(cases, testCase{o, stored, creators, updaters, subresource})
	}
	refs := []string{"none", "env", "envFrom", "init-env", "init-envFrom", "volume", "projected", "pull", "ephemeral-env", "ephemeral-envFrom"}
	for i, ref := range refs {
		for j, identity := range [][2]string{{namespace, "default"}, {namespace, mirror}, {"other", "default"}} {
			o := owned(pod(fmt.Sprintf("case-%d-%d", i, j), identity[0], identity[1], ref), "Job", "scheduled-job")
			subresource := ""
			if strings.HasPrefix(ref, "ephemeral-") {
				subresource = "ephemeralcontainers"
			}
			allowed := []string{}
			if j == 2 || j == 0 && ref == "none" {
				allowed = actors
			}
			if j == 1 && subresource == "" {
				allowed = []string{jobController}
			}
			updaters := append([]string{}, allowed...)
			if subresource == "" {
				updaters = append(updaters, garbageCollector)
			}
			seed(o, allowed, updaters, subresource)
		}
	}
	// Wrong/missing ownership fails even for a controller caller.
	seed(pod("unowned", namespace, mirror, "env"), nil, []string{garbageCollector}, "")
	seed(owned(pod("wrong-owner", namespace, mirror, "env"), "CronJob", mirror), nil, []string{garbageCollector}, "")
	seed(batch("Job", "scheduled", mirror, "env"), []string{cronController}, []string{cronController, jobController, garbageCollector}, "")
	seed(batch("Job", "account-only", mirror, "none"), []string{cronController}, []string{cronController, jobController, garbageCollector}, "")
	seed(batch("Job", "wrong-account", "default", "env"), nil, []string{garbageCollector}, "")
	seed(owned(batch("Job", "wrong-cronjob", mirror, "env"), "CronJob", "attacker-cronjob"), nil, []string{garbageCollector}, "")
	seed(batch("Job", "unrelated", "default", "none"), actors, actors, "")
	seed(batch("CronJob", mirror, mirror, "env"), []string{argoController}, []string{argoController}, "")
	seed(batch("CronJob", "attacker-cronjob", mirror, "env"), nil, nil, "")
	seed(batch("CronJob", "unrelated", "default", "none"), actors, actors, "")

	command := exec.CommandContext(ctx, input(t, "HELM_BIN"), "template", namespace,
		filepath.Dir(input(t, "MIRROR_CHART")), "--namespace", namespace)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	output, err := command.Output()
	if err != nil {
		t.Fatalf("helm: %v: %s", err, stderr.String())
	}
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(output), 4096)
	var policies []*unstructured.Unstructured
	var authorizations []*unstructured.Unstructured
	namespaceObjects := map[string]int{}
	for {
		var o unstructured.Unstructured
		err := decoder.Decode(&o)
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		switch o.GetKind() {
		case "ResourceQuota", "LimitRange":
			if o.GetNamespace() != namespace {
				t.Fatalf("%s rendered in namespace %q, want %q", o.GetKind(), o.GetNamespace(), namespace)
			}
			namespaceObjects[o.GetKind()]++
		case "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding":
			policies = append(policies, &o)
		case "ConfigMap", "Secret", "Role", "RoleBinding":
			if o.GetNamespace() == "" {
				o.SetNamespace(namespace)
			}
			stored := create(&o)
			if o.GetKind() == "ConfigMap" {
				authorizations = append(authorizations, stored)
			}
		}
	}
	if namespaceObjects["ResourceQuota"] != 1 || namespaceObjects["LimitRange"] != 1 {
		t.Fatalf("want one quota and one limit range, got %v", namespaceObjects)
	}
	if len(policies) != 6 {
		t.Fatalf("want three policy/binding pairs, got %d", len(policies))
	}
	// Exercise the actual rendered RBAC using the garage-init identity.
	garage := clients["system:serviceaccount:garage:garage-init"]
	secretGVR := schema.GroupVersionResource{Version: "v1", Resource: "secrets"}
	configGVR := schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}
	claims := []string{"factory-public-factory-public-s3", mirror + "-s3"}
	deadline := time.Now().Add(10 * time.Second)
	for {
		_, err := garage.Resource(secretGVR).Namespace(namespace).Get(ctx, claims[0], metav1.GetOptions{})
		if err == nil {
			break
		}
		if !apierrors.IsForbidden(err) || time.Now().After(deadline) {
			t.Fatal(err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	for _, name := range claims {
		o, err := garage.Resource(secretGVR).Namespace(namespace).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		o.SetAnnotations(map[string]string{"test": "update"})
		if _, err := garage.Resource(secretGVR).Namespace(namespace).Update(ctx, o, metav1.UpdateOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := garage.Resource(configGVR).Namespace("garage").List(ctx, metav1.ListOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := garage.Resource(configGVR).Namespace(namespace).List(ctx, metav1.ListOptions{}); !apierrors.IsForbidden(err) {
		t.Fatalf("consumer ConfigMap discovery must be forbidden: %v", err)
	}
	for _, o := range authorizations {
		if _, err := resource(garage, o).Update(ctx, o, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}}); !apierrors.IsForbidden(err) {
			t.Fatalf("garage-init must not write authorizations: %v", err)
		}
	}
	if _, err := garage.Resource(secretGVR).Namespace(namespace).List(ctx, metav1.ListOptions{}); !apierrors.IsForbidden(err) {
		t.Fatalf("Secret list must be forbidden: %v", err)
	}
	for _, name := range []string{secret, "unrelated-secret"} {
		o := create(object("Secret", name, namespace, nil))
		if _, err := garage.Resource(secretGVR).Namespace(namespace).Get(ctx, name, metav1.GetOptions{}); !apierrors.IsForbidden(err) {
			t.Fatalf("get %s: %v", name, err)
		}
		if _, err := garage.Resource(secretGVR).Namespace(namespace).Update(ctx, o, metav1.UpdateOptions{}); !apierrors.IsForbidden(err) {
			t.Fatalf("update %s: %v", name, err)
		}
	}
	t.Run("claim-authorization", func(t *testing.T) {
		testClaimAuthorization(t, ctx, client, clients, authorizations)
	})
	for _, o := range policies {
		create(o)
	}
	// Require an actual denial from each policy before the complete matrix.
	for _, o := range []*unstructured.Unstructured{
		owned(pod("ready", namespace, mirror, "env"), "Job", "scheduled"),
		batch("Job", "ready", mirror, "env"), batch("CronJob", "ready", mirror, "env"),
	} {
		deadline := time.Now().Add(30 * time.Second)
		for {
			_, err := resource(clients[attacker], o).Create(ctx, o, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
			if denial(err) {
				break
			}
			if err != nil || time.Now().After(deadline) {
				t.Fatalf("%s policy not effective: %v", o.GetKind(), err)
			}
			time.Sleep(50 * time.Millisecond)
		}
	}
	contains := func(users []string, user string) bool {
		for _, item := range users {
			if item == user {
				return true
			}
		}
		return false
	}
	for _, tc := range cases {
		t.Run(tc.object.GetKind()+"/"+tc.object.GetName(), func(t *testing.T) {
			for _, user := range actors {
				t.Run(user, func(t *testing.T) {
					check := func(err error, allowed bool) {
						t.Helper()
						if allowed && err != nil {
							t.Fatalf("positive control rejected: %v", err)
						}
						if !allowed && !denial(err) {
							t.Fatalf("want mirror policy denial, got %v", err)
						}
					}
					if user == garbageCollector && tc.subresource == "" {
						metadataOnly := tc.stored.DeepCopy()
						metadataOnly.SetAnnotations(map[string]string{"gc": "metadata"})
						_, err := resource(clients[user], metadataOnly).Update(ctx, metadataOnly, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}})
						if err != nil {
							t.Fatalf("metadata-only garbage collection: %v", err)
						}
					}
					update := tc.stored.DeepCopy()
					update.SetAnnotations(map[string]string{"test": "update"})
					if update.GetKind() == "CronJob" {
						update.Object["spec"].(map[string]interface{})["suspend"] = false
					}
					subresources := []string{}
					if tc.subresource != "" {
						subresources = append(subresources, tc.subresource)
					}
					_, err := resource(clients[user], update).Update(ctx, update, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}}, subresources...)
					check(err, contains(tc.updaters, user))
				})
			}
			// After testing UPDATE, delete the seed so CREATE can succeed
			// with the exact reserved CronJob name. All creates are dry runs.
			// Jobs default to orphan propagation, which leaves a finalizer
			// pending forever without a garbage collector in envtest.
			if tc.subresource == "" {
				zero := int64(0)
				background := metav1.DeletePropagationBackground
				if err := resource(client, tc.stored).Delete(ctx, tc.stored.GetName(), metav1.DeleteOptions{
					GracePeriodSeconds: &zero, PropagationPolicy: &background,
				}); err != nil {
					t.Fatal(err)
				}
				if _, err := resource(client, tc.stored).Get(ctx, tc.stored.GetName(), metav1.GetOptions{}); !apierrors.IsNotFound(err) {
					t.Fatalf("seed must be deleted before CREATE checks: %v", err)
				}
				for _, user := range actors {
					t.Run("create/"+user, func(t *testing.T) {
						_, err := resource(clients[user], tc.object).Create(ctx, tc.object, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
						if contains(tc.creators, user) {
							if err != nil {
								t.Fatalf("positive create rejected: %v", err)
							}
						} else if !denial(err) {
							t.Fatalf("want mirror policy denial, got %v", err)
						}
					})
				}
			}
		})
	}
}

func testClaimAuthorization(t *testing.T, ctx context.Context, admin dynamic.Interface, clients map[string]dynamic.Interface, claims []*unstructured.Unstructured) {
	t.Helper()
	const policy = "garage-claim-authorizations"
	const refusal = "Only ArgoCD may change Garage claim authorizations."
	create := func(o *unstructured.Unstructured) {
		t.Helper()
		if _, err := resource(admin, o).Create(ctx, o, metav1.CreateOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	// Deliberately grant the attacker stronger RBAC than a consumer has in
	// production. Admission must still reject writes in Garage's namespace.
	// The garbage collector gets the same rights it has in production.
	create(object("Role", "claim-editors", "garage", map[string]interface{}{"rules": []interface{}{map[string]interface{}{
		"apiGroups": []interface{}{""}, "resources": []interface{}{"configmaps"},
		"verbs": []interface{}{"create", "update", "patch", "delete"},
	}}}))
	create(object("RoleBinding", "claim-editors", "garage", map[string]interface{}{
		"roleRef": map[string]interface{}{"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "claim-editors"},
		"subjects": []interface{}{
			map[string]interface{}{"kind": "User", "name": attacker, "apiGroup": "rbac.authorization.k8s.io"},
			map[string]interface{}{"kind": "User", "name": argoController, "apiGroup": "rbac.authorization.k8s.io"},
			map[string]interface{}{"kind": "User", "name": garbageCollector, "apiGroup": "rbac.authorization.k8s.io"},
		},
	}))
	for _, o := range readObjects(t, "GARAGE_CLAIM_GUARD") {
		create(o)
	}
	if len(claims) != 2 {
		t.Fatalf("want two rendered authorizations, got %d", len(claims))
	}
	claim := claims[0]
	deadline := time.Now().Add(30 * time.Second)
	for {
		_, err := resource(clients[attacker], claim).Update(ctx, claim, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}})
		if policyDenial(err, policy, refusal) {
			break
		}
		if err != nil || time.Now().After(deadline) {
			t.Fatalf("claim authorization guard not effective: %v", err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	for _, original := range claims {
		for _, field := range []string{"bucket", "access", "namespace", "secretName", "labels"} {
			t.Run(original.GetName()+"/"+field, func(t *testing.T) {
				update := original.DeepCopy()
				if field == "labels" {
					update.SetLabels(nil)
				} else {
					value := map[string]string{"bucket": "factory-db-backups", "access": "readwrite", "namespace": "factory-db", "secretName": "factory-db-s3"}[field]
					if err := unstructured.SetNestedField(update.Object, value, "data", field); err != nil {
						t.Fatal(err)
					}
				}
				for _, user := range []string{attacker, garbageCollector, argoController} {
					_, err := resource(clients[user], update).Update(ctx, update, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}})
					if user != argoController && !policyDenial(err, policy, refusal) || user == argoController && err != nil {
						t.Fatalf("%s update: %v", user, err)
					}
				}
			})
		}
		for _, user := range []string{attacker, garbageCollector, argoController} {
			err := resource(clients[user], original).Delete(ctx, original.GetName(), metav1.DeleteOptions{DryRun: []string{metav1.DryRunAll}})
			if user != argoController && !policyDenial(err, policy, refusal) || user == argoController && err != nil {
				t.Fatalf("%s delete authorization: %v", user, err)
			}
		}
	}
	for _, variant := range []string{"bound", "label-only", "name-only", "unrelated", "consumer"} {
		o := claim.DeepCopy()
		o.SetResourceVersion("")
		o.SetUID("")
		o.SetManagedFields(nil)
		name := "future-publisher-s3"
		_ = unstructured.SetNestedField(o.Object, name, "data", "secretName")
		o.SetName(fmt.Sprintf("garage-claim-%x", sha256.Sum256([]byte(namespace+"/"+name))))
		if variant == "label-only" || variant == "unrelated" {
			o.SetName("ordinary-config")
		}
		if variant == "name-only" || variant == "unrelated" {
			o.SetLabels(nil)
		}
		if variant == "consumer" {
			o.SetNamespace(namespace)
			_ = unstructured.SetNestedField(o.Object, "factory-db-backups", "data", "bucket")
			_ = unstructured.SetNestedField(o.Object, "readwrite", "data", "access")
		}
		for _, user := range []string{attacker, garbageCollector, argoController} {
			_, err := resource(clients[user], o).Create(ctx, o, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
			allowed := user == argoController || variant == "unrelated" || variant == "consumer"
			if allowed && err != nil || !allowed && !policyDenial(err, policy, refusal) {
				t.Fatalf("%s create %s: %v", user, variant, err)
			}
		}
	}
	t.Run("garbage-collection", func(t *testing.T) {
		testClaimGarbageCollection(t, ctx, admin, clients, claim)
	})
}

// ArgoCD prunes with foreground propagation. The API server then keeps the
// claim with the foregroundDeletion finalizer until the garbage collector
// removes it (HOME-492). The collector may remove only that finalizer from a
// claim that is being deleted. It must keep the other finalizers in their
// order and change nothing else.
func testClaimGarbageCollection(t *testing.T, ctx context.Context, admin dynamic.Interface, clients map[string]dynamic.Interface, claim *unstructured.Unstructured) {
	const policy = "garage-claim-authorizations"
	const refusal = "Only ArgoCD may change Garage claim authorizations."
	const foreground = metav1.FinalizerDeleteDependents
	const keep, hold = "example.com/keep", "example.com/hold"
	dryRun := metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}}
	owner := metav1.OwnerReference{
		APIVersion: "v1", Kind: "ConfigMap", Name: "claim-owner",
		UID: "00000000-0000-4000-8000-000000000002",
	}
	// prune lets ArgoCD create a claim and delete it. envtest runs no garbage
	// collector, so the claim stays with its finalizers.
	prune := func(secretName string, finalizers []string, owners []metav1.OwnerReference, propagation metav1.DeletionPropagation) *unstructured.Unstructured {
		t.Helper()
		o := claim.DeepCopy()
		o.SetResourceVersion("")
		o.SetUID("")
		o.SetManagedFields(nil)
		_ = unstructured.SetNestedField(o.Object, secretName, "data", "secretName")
		o.SetName(fmt.Sprintf("garage-claim-%x", sha256.Sum256([]byte(namespace+"/"+secretName))))
		o.SetFinalizers(finalizers)
		o.SetOwnerReferences(owners)
		argo := resource(clients[argoController], o)
		if _, err := argo.Create(ctx, o, metav1.CreateOptions{}); err != nil {
			t.Fatal(err)
		}
		if err := argo.Delete(ctx, o.GetName(), metav1.DeleteOptions{PropagationPolicy: &propagation}); err != nil {
			t.Fatal(err)
		}
		deleting, err := resource(admin, o).Get(ctx, o.GetName(), metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		if deleting.GetDeletionTimestamp() == nil {
			t.Fatalf("want a deleting claim %s, got finalizers %v", secretName, deleting.GetFinalizers())
		}
		return deleting
	}
	gone := func(o *unstructured.Unstructured) {
		t.Helper()
		deadline := time.Now().Add(10 * time.Second)
		for {
			_, err := resource(admin, o).Get(ctx, o.GetName(), metav1.GetOptions{})
			if apierrors.IsNotFound(err) {
				return
			}
			if time.Now().After(deadline) {
				t.Fatalf("deleting claim %s must go away after its finalizers go: %v", o.GetName(), err)
			}
			time.Sleep(50 * time.Millisecond)
		}
	}
	withFinalizers := func(o *unstructured.Unstructured, finalizers ...string) *unstructured.Unstructured {
		c := o.DeepCopy()
		c.SetFinalizers(finalizers)
		return c
	}
	deny := func(change string, o *unstructured.Unstructured) {
		t.Helper()
		if _, err := resource(clients[garbageCollector], o).Update(ctx, o, dryRun); !policyDenial(err, policy, refusal) {
			t.Fatalf("garbage collector %s: %v", change, err)
		}
	}
	// removeForeground sends the merge patch that the garbage collector sends:
	// the other finalizers in their order, or null, with the resource version.
	removeForeground := func(o *unstructured.Unstructured) {
		t.Helper()
		var rest []string
		for _, f := range o.GetFinalizers() {
			if f != foreground {
				rest = append(rest, f)
			}
		}
		patch, err := json.Marshal(map[string]interface{}{"metadata": map[string]interface{}{
			"resourceVersion": o.GetResourceVersion(), "finalizers": rest,
		}})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := resource(clients[garbageCollector], o).Patch(ctx, o.GetName(), types.MergePatchType, patch, metav1.PatchOptions{}); err != nil {
			t.Fatalf("garbage collector foregroundDeletion removal from %s: %v", o.GetName(), err)
		}
	}
	// ArgoCD stands in for the controller that owns the other finalizers.
	release := func(o *unstructured.Unstructured) {
		t.Helper()
		patch := []byte(`{"metadata":{"finalizers":null}}`)
		if _, err := resource(clients[argoController], o).Patch(ctx, o.GetName(), types.MergePatchType, patch, metav1.PatchOptions{}); err != nil {
			t.Fatalf("argocd finalizer removal from %s: %v", o.GetName(), err)
		}
	}

	// A pruned claim with only the foregroundDeletion finalizer.
	pruned := prune("pruned-s3", nil, nil, metav1.DeletePropagationForeground)
	if !slices.Equal(pruned.GetFinalizers(), []string{foreground}) {
		t.Fatalf("want only the foregroundDeletion finalizer, got %v", pruned.GetFinalizers())
	}
	edits := map[string]func(*unstructured.Unstructured){
		"data": func(o *unstructured.Unstructured) {
			_ = unstructured.SetNestedField(o.Object, "factory-db-backups", "data", "bucket")
		},
		"binaryData": func(o *unstructured.Unstructured) {
			_ = unstructured.SetNestedField(o.Object, "eA==", "binaryData", "extra")
		},
		"labels": func(o *unstructured.Unstructured) { o.SetLabels(nil) },
		"added-label": func(o *unstructured.Unstructured) {
			labels := o.GetLabels()
			if labels == nil {
				labels = map[string]string{}
			}
			labels["extra"] = "x"
			o.SetLabels(labels)
		},
		"annotations":     func(o *unstructured.Unstructured) { o.SetAnnotations(map[string]string{"gc": "metadata"}) },
		"ownerReferences": func(o *unstructured.Unstructured) { o.SetOwnerReferences([]metav1.OwnerReference{owner}) },
		"immutable":       func(o *unstructured.Unstructured) { o.Object["immutable"] = true },
	}
	for field, edit := range edits {
		o := withFinalizers(pruned)
		edit(o)
		deny("changed "+field+" while it removed foregroundDeletion", o)
	}
	// The finalizers stay the same here, so the update removes no finalizer.
	ownerOnly := pruned.DeepCopy()
	ownerOnly.SetOwnerReferences([]metav1.OwnerReference{owner})
	deny("changed only the ownerReferences of a deleting claim", ownerOnly)
	for _, user := range []string{attacker, argoController} {
		_, err := resource(clients[user], pruned).Update(ctx, withFinalizers(pruned), dryRun)
		if user == attacker && !policyDenial(err, policy, refusal) || user == argoController && err != nil {
			t.Fatalf("%s finalizer removal: %v", user, err)
		}
	}
	// A claim that is not being deleted stays ArgoCD-only for the collector.
	live, err := resource(admin, claim).Get(ctx, claim.GetName(), metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	live.SetFinalizers([]string{"factory.jomcgi.dev/hold"})
	deny("updated a live claim", live)
	removeForeground(pruned)
	gone(pruned)

	// A pruned claim with other finalizers and an owner reference. The
	// collector may remove only foregroundDeletion, and the claim stays until
	// the other finalizers go.
	kept := prune("kept-s3", []string{keep, hold}, []metav1.OwnerReference{owner}, metav1.DeletePropagationForeground)
	if !slices.Equal(kept.GetFinalizers(), []string{keep, hold, foreground}) {
		t.Fatalf("want the other finalizers before foregroundDeletion, got %v", kept.GetFinalizers())
	}
	deny("removed a different finalizer", withFinalizers(kept, hold, foreground))
	deny("removed a different finalizer with foregroundDeletion", withFinalizers(kept, hold))
	deny("reordered the other finalizers", withFinalizers(kept, hold, keep))
	// The API server refuses a new finalizer on a deleting object before
	// admission runs. The policy refuses it too.
	_, err = resource(clients[garbageCollector], kept).Update(ctx, withFinalizers(kept, keep, hold, "example.com/extra"), dryRun)
	if !policyDenial(err, policy, refusal) && !(apierrors.IsInvalid(err) && strings.Contains(err.Error(), "no new finalizers")) {
		t.Fatalf("garbage collector added a finalizer with foregroundDeletion removal: %v", err)
	}
	unowned := kept.DeepCopy()
	unowned.SetOwnerReferences(nil)
	deny("removed an owner reference", unowned)
	unowned.SetFinalizers([]string{keep, hold})
	deny("removed an owner reference with foregroundDeletion", unowned)
	removeForeground(kept)
	stays, err := resource(admin, kept).Get(ctx, kept.GetName(), metav1.GetOptions{})
	if err != nil {
		t.Fatalf("claim with other finalizers must stay: %v", err)
	}
	if stays.GetDeletionTimestamp() == nil || !slices.Equal(stays.GetFinalizers(), []string{keep, hold}) ||
		len(stays.GetOwnerReferences()) != 1 {
		t.Fatalf("want the other finalizers and the owner reference to stay, got %v %v", stays.GetFinalizers(), stays.GetOwnerReferences())
	}
	release(stays)
	gone(stays)

	// A deleting claim without the foregroundDeletion finalizer.
	background := prune("background-s3", []string{keep}, nil, metav1.DeletePropagationBackground)
	if !slices.Equal(background.GetFinalizers(), []string{keep}) {
		t.Fatalf("want only the %s finalizer, got %v", keep, background.GetFinalizers())
	}
	deny("updated a deleting claim without foregroundDeletion", background.DeepCopy())
	deny("removed a different finalizer without foregroundDeletion", withFinalizers(background))
	release(background)
	gone(background)
}
