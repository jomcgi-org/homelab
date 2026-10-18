package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/yaml"

	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
)

const chartDir = "../helm/oci-model-cache-operator"

// Use the same Helm binary and raw chart runfiles as the repository's render
// tests. Decode the Deployment into Kubernetes types so boolean env values fail.
func renderOperator(t *testing.T, values ...string) (*appsv1.Deployment, map[string][]map[string]any) {
	t.Helper()
	helm := os.Getenv("HELM_BIN")
	if helm == "" {
		helm = "helm"
	} else if !filepath.IsAbs(helm) && os.Getenv("TEST_SRCDIR") != "" {
		helm = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), helm)
	}
	args := []string{"template", "cache-test", chartDir, "--namespace", "cache-test", "--include-crds"}
	for _, value := range values {
		option := "--set-string"
		if strings.HasPrefix(value, "webhook.enabled=") || strings.HasPrefix(value, "syncServiceAccount.create=") {
			option = "--set"
		}
		args = append(args, option, value)
	}
	output, err := exec.Command(helm, args...).CombinedOutput()
	if err != nil {
		t.Fatalf("helm template: %v\n%s", err, output)
	}
	resources := map[string][]map[string]any{}
	var deployment *appsv1.Deployment
	decoder := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(output), 4096)
	for {
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err == io.EOF {
			break
		} else if err != nil {
			t.Fatal(err)
		}
		var resource map[string]any
		if err := json.Unmarshal(raw, &resource); err != nil {
			t.Fatal(err)
		}
		if len(resource) == 0 {
			continue
		}
		kind := resource["kind"].(string)
		resources[kind] = append(resources[kind], resource)
		if kind == "Deployment" {
			if deployment != nil {
				t.Fatal("multiple Deployments")
			}
			deployment = &appsv1.Deployment{}
			if err := json.Unmarshal(raw, deployment); err != nil {
				t.Fatalf("invalid Kubernetes Deployment: %v", err)
			}
		}
	}
	if deployment == nil || len(deployment.Spec.Template.Spec.Containers) != 1 {
		t.Fatal("expected one manager container")
	}
	return deployment, resources
}

func managerEnv(container corev1.Container) map[string]string {
	result := map[string]string{}
	for _, env := range container.Env {
		if env.ValueFrom == nil {
			result[env.Name] = env.Value
		}
	}
	return result
}

func TestChartWebhookModes(t *testing.T) {
	for _, mode := range []string{"default", "true", "false"} {
		t.Run(mode, func(t *testing.T) {
			var values []string
			if mode != "default" {
				// Booleans must be parsed by Helm as booleans, not nonempty strings.
				values = append(values, "webhook.enabled="+mode)
			}
			deployment, resources := renderOperator(t, values...)
			pod := deployment.Spec.Template.Spec
			container := pod.Containers[0]
			enabled := mode != "false"
			if got := managerEnv(container)["ENABLE_WEBHOOKS"] != "false"; got != enabled {
				t.Fatalf("webhook.enabled=%s renders ENABLE_WEBHOOKS=%q", mode, managerEnv(container)["ENABLE_WEBHOOKS"])
			}
			for _, kind := range []string{"Service", "MutatingWebhookConfiguration", "Certificate", "Issuer"} {
				want := 0
				if enabled {
					want = 1
				}
				if len(resources[kind]) != want {
					t.Errorf("%s count = %d, want %d", kind, len(resources[kind]), want)
				}
			}
			if len(resources["CustomResourceDefinition"]) != 1 {
				t.Fatal("Helm must still include crds/ in both modes")
			}
			if !enabled {
				if len(container.Ports)+len(container.VolumeMounts)+len(pod.Volumes) != 0 {
					t.Fatal("disabled webhook retains ports or certificates")
				}
				return
			}
			if len(container.Ports) != 1 || container.Ports[0].ContainerPort != 9443 {
				t.Fatalf("webhook ports: %+v", container.Ports)
			}
			if len(container.VolumeMounts) != 1 || container.VolumeMounts[0].MountPath != "/tmp/k8s-webhook-server/serving-certs" || !container.VolumeMounts[0].ReadOnly {
				t.Fatalf("webhook mount: %+v", container.VolumeMounts)
			}
			if len(pod.Volumes) != 1 || pod.Volumes[0].Secret == nil || pod.Volumes[0].Name != container.VolumeMounts[0].Name {
				t.Fatalf("webhook volume: %+v", pod.Volumes)
			}
			cert := resources["Certificate"][0]["spec"].(map[string]any)
			if cert["secretName"] != pod.Volumes[0].Secret.SecretName {
				t.Fatal("certificate does not supply the mounted secret")
			}
			service := resources["Service"][0]
			port := service["spec"].(map[string]any)["ports"].([]any)[0].(map[string]any)
			if port["port"] != float64(443) || port["targetPort"] != float64(9443) {
				t.Fatalf("webhook service port: %v", port)
			}
			hook := resources["MutatingWebhookConfiguration"][0]["webhooks"].([]any)[0].(map[string]any)
			client := hook["clientConfig"].(map[string]any)["service"].(map[string]any)
			if client["name"] != service["metadata"].(map[string]any)["name"] || client["namespace"] != "cache-test" || client["path"] != "/mutate-v1-pod" {
				t.Fatalf("webhook client service: %v", client)
			}
		})
	}
}

func TestChartFixedHealthProbes(t *testing.T) {
	// The chart no longer advertises an override. Even legacy supplied values,
	// including disabled, host-bound and IPv6 addresses, cannot desync its probes.
	for _, oldAddress := range []string{":8081", ":9091", "127.0.0.1:9091", "[::]:9091", "0", ""} {
		t.Run(oldAddress, func(t *testing.T) {
			deployment, _ := renderOperator(t, "controllerManager.healthProbeBindAddress="+oldAddress)
			container := deployment.Spec.Template.Spec.Containers[0]
			var address string
			for _, arg := range container.Args {
				if strings.HasPrefix(arg, "--health-probe-bind-address=") {
					address = strings.TrimPrefix(arg, "--health-probe-bind-address=")
				}
			}
			if address != ":8081" {
				t.Fatalf("chart health address = %q, want fixed :8081", address)
			}
			_, portText, err := net.SplitHostPort(address)
			if err != nil {
				t.Fatal(err)
			}
			port, err := strconv.Atoi(portText)
			if err != nil {
				t.Fatal(err)
			}
			for path, probe := range map[string]*corev1.Probe{"/healthz": container.LivenessProbe, "/readyz": container.ReadinessProbe} {
				if probe == nil || probe.HTTPGet == nil || probe.HTTPGet.Path != path || probe.HTTPGet.Port.IntValue() != port {
					t.Fatalf("%s probe disagrees with manager address %s: %+v", path, address, probe)
				}
			}
		})
	}
}

func TestChartAdvertisesOnlySupportedValues(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(chartDir, "values.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]any
	if err := yaml.NewYAMLOrJSONDecoder(bytes.NewReader(data), 4096).Decode(&values); err != nil {
		t.Fatal(err)
	}
	manager := values["controllerManager"].(map[string]any)
	for key, section := range map[string]map[string]any{
		"healthProbeBindAddress": manager,
		"syncServiceAccount":     manager["env"].(map[string]any),
		"crds":                   values,
	} {
		if _, exists := section[key]; exists {
			t.Errorf("unsupported value %s is still advertised", key)
		}
	}
}

func TestChartSyncServiceAccount(t *testing.T) {
	for _, create := range []string{"true", "false"} {
		t.Run(create, func(t *testing.T) {
			deployment, resources := renderOperator(t, "syncServiceAccount.name=download-jobs", "syncServiceAccount.create="+create)
			t.Setenv("SYNC_SERVICE_ACCOUNT", managerEnv(deployment.Spec.Template.Spec.Containers[0])["SYNC_SERVICE_ACCOUNT"])
			var cfg config.Config
			cfg.BindFlags(flag.NewFlagSet("chart", flag.ContinueOnError))
			if cfg.SyncServiceAccount != "download-jobs" {
				t.Fatalf("sync service account = %q", cfg.SyncServiceAccount)
			}
			found := false
			for _, account := range resources["ServiceAccount"] {
				found = found || account["metadata"].(map[string]any)["name"] == cfg.SyncServiceAccount
			}
			if found != (create == "true") {
				t.Fatalf("sync service account created = %v, want %s", found, create)
			}
		})
	}
}

// Re-exec the test binary to exercise the real main setup, including the real
// controller-runtime manager, reconciler, sweeper and webhook registration guard.
func TestManagerProcess(t *testing.T) {
	if os.Getenv("OCI_CACHE_TEST_MAIN") != "1" {
		return
	}
	os.Args = append([]string{os.Args[0]}, flag.Args()...)
	main()
}

func TestChartManagerWithoutCertificates(t *testing.T) {
	certPath := "/tmp/k8s-webhook-server/serving-certs/tls.crt"
	if _, err := os.Stat(certPath); !os.IsNotExist(err) {
		t.Fatalf("test requires absent webhook certificates at %s, stat: %v", certPath, err)
	}
	for _, mode := range []string{"default", "false"} {
		t.Run(mode, func(t *testing.T) {
			var values []string
			if mode == "false" {
				values = append(values, "webhook.enabled=false")
			}
			deployment, _ := renderOperator(t, values...)
			api, watched := fakeOperatorAPI(t)
			kubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
			content := fmt.Sprintf("apiVersion: v1\nkind: Config\nclusters:\n- name: local\n  cluster:\n    server: %s\ncontexts:\n- name: local\n  context:\n    cluster: local\n    user: local\ncurrent-context: local\nusers:\n- name: local\n  user: {}\n", api.URL)
			if err := os.WriteFile(kubeconfig, []byte(content), 0600); err != nil {
				t.Fatal(err)
			}
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			probeAddress := listener.Addr().String()
			listener.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			defer cancel()
			args := append([]string{"-test.run=^TestManagerProcess$", "--"}, deployment.Spec.Template.Spec.Containers[0].Args...)
			// No cluster credentials, leader lock or metrics auth are needed for this
			// local API fixture. Use a dynamic loopback probe port to avoid collisions.
			args = append(args, "--leader-elect=false", "--metrics-bind-address=0", "--health-probe-bind-address="+probeAddress)
			cmd := exec.CommandContext(ctx, os.Args[0], args...)
			cmd.Env = []string{"OCI_CACHE_TEST_MAIN=1", "KUBECONFIG=" + kubeconfig, "OTEL_SDK_DISABLED=true"}
			for key, value := range managerEnv(deployment.Spec.Template.Spec.Containers[0]) {
				cmd.Env = append(cmd.Env, key+"="+value)
			}
			var output bytes.Buffer
			cmd.Stdout, cmd.Stderr = &output, &output
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			if mode == "default" {
				err := <-done
				if err == nil || ctx.Err() != nil || !strings.Contains(output.String(), "open "+certPath+": no such file or directory") {
					t.Fatalf("enabled manager must try to load its missing certificate: %v\n%s", err, &output)
				}
				return
			}
			ticker := time.NewTicker(20 * time.Millisecond)
			defer ticker.Stop()
			client := &http.Client{Timeout: 200 * time.Millisecond}
			for {
				select {
				case err := <-done:
					t.Fatalf("disabled manager exited before serving probes and starting watches: %v\n%s", err, &output)
				case <-ticker.C:
					if !watched() {
						continue
					}
					healthy := true
					for _, path := range []string{"/healthz", "/readyz"} {
						resp, err := client.Get("http://" + probeAddress + path)
						if err != nil {
							healthy = false
							break
						}
						resp.Body.Close()
						healthy = healthy && resp.StatusCode == http.StatusOK
					}
					if healthy {
						if err := cmd.Process.Signal(os.Interrupt); err != nil {
							t.Fatal(err)
						}
						if err := <-done; err != nil {
							t.Fatalf("manager shutdown: %v\n%s", err, &output)
						}
						return
					}
				}
			}
		})
	}
}

// Serve discovery plus empty list/watch streams for the three resources the
// real reconciler watches. No Kubernetes server, stored objects or credentials.
func fakeOperatorAPI(t *testing.T) (*httptest.Server, func() bool) {
	t.Helper()
	types := map[string]struct{ version, kind string }{
		"/api/v1/pods":        {"v1", "Pod"},
		"/apis/batch/v1/jobs": {"batch/v1", "Job"},
		"/apis/oci-model-cache.jomcgi.dev/v1alpha1/modelcaches": {"oci-model-cache.jomcgi.dev/v1alpha1", "ModelCache"},
	}
	var mu sync.Mutex
	watches := map[string]bool{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		encoder := json.NewEncoder(w)
		switch r.URL.Path {
		case "/api":
			_ = encoder.Encode(metav1.APIVersions{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "APIVersions"}, Versions: []string{"v1"}})
			return
		case "/apis":
			groups := metav1.APIGroupList{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "APIGroupList"}}
			for _, typ := range types {
				group, version, found := strings.Cut(typ.version, "/")
				if found {
					gv := metav1.GroupVersionForDiscovery{GroupVersion: typ.version, Version: version}
					groups.Groups = append(groups.Groups, metav1.APIGroup{Name: group, Versions: []metav1.GroupVersionForDiscovery{gv}, PreferredVersion: gv})
				}
			}
			_ = encoder.Encode(groups)
			return
		}
		for path, typ := range types {
			if r.URL.Path == path[:strings.LastIndex(path, "/")] {
				_ = encoder.Encode(metav1.APIResourceList{
					TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "APIResourceList"}, GroupVersion: typ.version,
					APIResources: []metav1.APIResource{{Name: filepath.Base(path), Kind: typ.kind, Namespaced: typ.kind != "ModelCache", Verbs: metav1.Verbs{"get", "list", "watch"}}},
				})
				return
			}
		}
		if typ, ok := types[r.URL.Path]; ok {
			if r.URL.Query().Get("watch") == "true" {
				mu.Lock()
				watches[r.URL.Path] = true
				mu.Unlock()
				if r.URL.Query().Get("sendInitialEvents") == "true" {
					_ = encoder.Encode(map[string]any{"type": "BOOKMARK", "object": map[string]any{
						"apiVersion": typ.version, "kind": typ.kind,
						"metadata": map[string]any{"resourceVersion": "1", "annotations": map[string]string{"k8s.io/initial-events-end": "true"}},
					}})
				}
				w.(http.Flusher).Flush()
				<-r.Context().Done()
				return
			}
			_ = encoder.Encode(map[string]any{"apiVersion": typ.version, "kind": typ.kind + "List", "metadata": map[string]string{"resourceVersion": "1"}, "items": []any{}})
			return
		}
		t.Errorf("unexpected local API request: %s %s", r.Method, r.URL)
		http.NotFound(w, r)
	}))
	t.Cleanup(server.Close)
	return server, func() bool {
		mu.Lock()
		defer mu.Unlock()
		return len(watches) == len(types)
	}
}
