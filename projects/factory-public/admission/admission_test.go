package admission_test

import (
	"bytes"
	"context"
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
	return err != nil && (apierrors.IsForbidden(err) || apierrors.IsInvalid(err)) &&
		strings.Contains(err.Error(), namespace+"-"+secret) &&
		strings.Contains(err.Error(), "denied request") && strings.Contains(err.Error(), message)
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
			"apiGroups": []interface{}{"", "batch"}, "resources": []interface{}{"pods", "pods/ephemeralcontainers", "jobs", "cronjobs"},
			"verbs": []interface{}{"create", "update"},
		}}}))
		subjects := []interface{}{}
		for _, user := range actors {
			subjects = append(subjects, map[string]interface{}{"kind": "User", "name": user, "apiGroup": "rbac.authorization.k8s.io"})
		}
		create(object("RoleBinding", "test-creators", ns, map[string]interface{}{
			"subjects": subjects, "roleRef": map[string]interface{}{"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "test-creators"},
		}))
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
		case "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding":
			policies = append(policies, &o)
		case "ConfigMap", "Secret", "Role", "RoleBinding":
			o.SetNamespace(namespace)
			create(&o)
		}
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
	if _, err := garage.Resource(configGVR).Namespace(namespace).List(ctx, metav1.ListOptions{}); err != nil {
		t.Fatal(err)
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
