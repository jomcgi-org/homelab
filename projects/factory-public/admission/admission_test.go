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
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

const (
	namespace = "factory-public"
	mirror    = "factory-public-factory-public-mirror"
	secret    = mirror + "-r2"
	message   = "Only this mirror ServiceAccount may use its R2 Secret."
)

var pods = schema.GroupVersionResource{Version: "v1", Resource: "pods"}

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
	config, err := environment.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := environment.Stop(); err != nil {
			t.Error(err)
		}
	})
	client, err := dynamic.NewForConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	for _, ns := range []string{namespace, "other"} {
		object := &unstructured.Unstructured{Object: map[string]interface{}{
			"apiVersion": "v1", "kind": "Namespace", "metadata": map[string]interface{}{"name": ns},
		}}
		if _, err := client.Resource(schema.GroupVersionResource{Version: "v1", Resource: "namespaces"}).Create(ctx, object, metav1.CreateOptions{}); err != nil {
			t.Fatal(err)
		}
		for _, account := range []string{"default", mirror} {
			object := &unstructured.Unstructured{Object: map[string]interface{}{
				"apiVersion": "v1", "kind": "ServiceAccount",
				"metadata": map[string]interface{}{"name": account, "namespace": ns},
			}}
			if _, err := client.Resource(schema.GroupVersionResource{Version: "v1", Resource: "serviceaccounts"}).Namespace(ns).Create(ctx, object, metav1.CreateOptions{}); err != nil {
				t.Fatal(err)
			}
		}
	}

	type testCase struct {
		object      *unstructured.Unstructured
		update      *unstructured.Unstructured
		deny        bool
		subresource string
	}
	var cases []testCase
	refs := []string{"none", "env", "envFrom", "init-env", "init-envFrom", "volume", "projected", "pull", "ephemeral-env", "ephemeral-envFrom"}
	for i, ref := range refs {
		for j, identity := range [][2]string{{namespace, "default"}, {namespace, mirror}, {"other", "default"}} {
			object := pod(fmt.Sprintf("case-%d-%d", i, j), identity[0], identity[1], ref)
			seed := object.DeepCopy()
			subresource := ""
			if strings.HasPrefix(ref, "ephemeral-") {
				unstructured.RemoveNestedField(seed.Object, "spec", "ephemeralContainers")
				subresource = "ephemeralcontainers"
			}
			// Every fixture must succeed before the policy is installed.
			stored, err := client.Resource(pods).Namespace(identity[0]).Create(ctx, seed, metav1.CreateOptions{})
			if err != nil {
				t.Fatalf("baseline %s: %v", ref, err)
			}
			if subresource != "" {
				containers, _, _ := unstructured.NestedSlice(object.Object, "spec", "ephemeralContainers")
				if err := unstructured.SetNestedSlice(stored.Object, containers, "spec", "ephemeralContainers"); err != nil {
					t.Fatal(err)
				}
				if _, err := client.Resource(pods).Namespace(identity[0]).Update(ctx, stored, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}}, subresource); err != nil {
					t.Fatalf("baseline debug container: %v", err)
				}
			}
			object.SetName(object.GetName() + "-create")
			cases = append(cases, testCase{object, stored, i != 0 && j == 0, subresource})
		}
	}

	command := exec.CommandContext(ctx, input(t, "HELM_BIN"), "template", namespace,
		filepath.Dir(input(t, "MIRROR_CHART")), "--namespace", namespace)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	output, err := command.Output()
	if err != nil {
		t.Fatalf("helm: %v: %s", err, stderr.String())
	}
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(output), 4096)
	installed := 0
	for {
		var object unstructured.Unstructured
		err := decoder.Decode(&object)
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		plural := map[string]string{
			"ValidatingAdmissionPolicy":        "validatingadmissionpolicies",
			"ValidatingAdmissionPolicyBinding": "validatingadmissionpolicybindings",
		}[object.GetKind()]
		if plural == "" {
			continue
		}
		gvr := schema.GroupVersionResource{Group: "admissionregistration.k8s.io", Version: "v1", Resource: plural}
		if _, err := client.Resource(gvr).Create(ctx, &object, metav1.CreateOptions{}); err != nil {
			t.Fatalf("install %s: %v", object.GetKind(), err)
		}
		installed++
	}
	if installed != 2 {
		t.Fatalf("want policy and binding, installed %d", installed)
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		_, err := client.Resource(pods).Namespace(namespace).Create(ctx, pod("ready", namespace, "default", "env"), metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
		if denial(err) {
			break
		}
		if err != nil || time.Now().After(deadline) {
			t.Fatalf("policy not effective: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
	for _, tc := range cases {
		t.Run(tc.object.GetName(), func(t *testing.T) {
			check := func(err error) {
				t.Helper()
				if tc.deny && !denial(err) {
					t.Fatalf("want mirror policy denial, got %v", err)
				}
				if !tc.deny && err != nil {
					t.Fatalf("positive control rejected: %v", err)
				}
			}
			resource := client.Resource(pods).Namespace(tc.object.GetNamespace())
			if tc.subresource == "" {
				_, err := resource.Create(ctx, tc.object, metav1.CreateOptions{DryRun: []string{metav1.DryRunAll}})
				check(err)
				tc.update.SetAnnotations(map[string]string{"test": "update"})
			}
			subresources := []string{}
			if tc.subresource != "" {
				subresources = append(subresources, tc.subresource)
			}
			_, err := resource.Update(ctx, tc.update, metav1.UpdateOptions{DryRun: []string{metav1.DryRunAll}}, subresources...)
			check(err)
		})
	}
}
