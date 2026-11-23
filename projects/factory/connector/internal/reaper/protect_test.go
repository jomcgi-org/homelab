package reaper

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func costOf(t *testing.T, client *fake.Clientset, ns, name string) string {
	t.Helper()
	p, err := client.CoreV1().Pods(ns).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return p.Annotations[deletionCost]
}

func TestProtectBusyWorkersMarksOnlyWorkersThatHostAnActor(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	busyPod := worker("busy", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, false)
	idle := worker("idle", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, false)
	freed := worker("freed", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, false)
	freed.Annotations = map[string]string{deletionCost: "1"} // its actor left since the last pass
	leaving := worker("leaving", "homelab-codex", corev1.PodRunning, corev1.ConditionTrue, true)
	other := worker("other", "homelab-reviewer", corev1.PodRunning, corev1.ConditionTrue, false)
	client := fake.NewSimpleClientset(busyPod, idle, freed, leaving, other)

	busy := map[Pool]map[string]struct{}{coder: {"busy": {}}}
	if err := ProtectBusyWorkers(context.Background(), client.CoreV1(), []Pool{coder}, busy); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"busy": "1", "idle": "", "freed": "0", "leaving": "", "other": ""} {
		if got := costOf(t, client, coder.Namespace, name); got != want {
			t.Fatalf("deletion cost of %s = %q, want %q", name, got, want)
		}
	}
	// A pass that changes nothing patches nothing.
	client.ClearActions()
	if err := ProtectBusyWorkers(context.Background(), client.CoreV1(), []Pool{coder}, busy); err != nil {
		t.Fatal(err)
	}
	for _, a := range client.Actions() {
		if a.GetVerb() == "patch" {
			t.Fatalf("a settled pass sent %v", a)
		}
	}
}

func TestBusyPodsNamesTheWorkersPerPool(t *testing.T) {
	coder := Pool{"factory-r-homelab", "homelab-codex"}
	got, err := BusyPods([][]byte{
		actorIn(coder.Namespace, coder.Name, "a", stateRunning),
		actorIn(coder.Namespace, coder.Name, "b", 4), // SUSPENDED
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := got[coder]["a"]; !ok || len(got[coder]) != 1 || len(got) != 1 {
		t.Fatalf("busy pods = %v, want only a", got)
	}
}
