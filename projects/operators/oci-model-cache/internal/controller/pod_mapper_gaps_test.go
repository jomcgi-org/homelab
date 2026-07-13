package controller

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TestPodToModelCacheRequests_EmptyAnnotationValue verifies that when the
// waiting-for annotation key is present but set to an empty string, the
// function returns nil (same as if the annotation were absent).
// The guard `if mcName == ""` ensures empty-value annotations are ignored.
func TestPodToModelCacheRequests_EmptyAnnotationValue(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: "default",
			Annotations: map[string]string{
				"oci-model-cache.jomcgi.dev/waiting-for": "",
			},
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	assert.Empty(t, requests, "empty annotation value should produce no reconcile requests")
}

// TestPodToModelCacheRequests_EmptyAnnotationsMap verifies that a pod with a
// non-nil but empty annotations map returns nil — map lookup for a missing key
// returns the zero value (""), triggering the early-return guard.
func TestPodToModelCacheRequests_EmptyAnnotationsMap(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:        "test-pod",
			Namespace:   "default",
			Annotations: map[string]string{}, // non-nil but empty
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	assert.Empty(t, requests, "non-nil empty annotations map should produce no reconcile requests")
}

// TestPodToModelCacheRequests_UnrelatedAnnotationsOnly verifies that a pod with
// annotations, none of which is the waiting-for key, produces no reconcile requests.
func TestPodToModelCacheRequests_UnrelatedAnnotationsOnly(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: "default",
			Annotations: map[string]string{
				"some.other/annotation":                            "value",
				"kubectl.kubernetes.io/last-applied-configuration": "{}",
			},
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	assert.Empty(t, requests, "unrelated annotations should produce no reconcile requests")
}

// TestPodToModelCacheRequests_NameOnly verifies that the returned request carries
// only the ModelCache name — no namespace — since ModelCache is a cluster-scoped
// resource and podToModelCacheRequests intentionally omits the namespace.
func TestPodToModelCacheRequests_NameOnly(t *testing.T) {
	const mcName = "my-cluster-model"

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "worker-pod",
			Namespace: "production",
			Annotations: map[string]string{
				"oci-model-cache.jomcgi.dev/waiting-for": mcName,
			},
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	require.Len(t, requests, 1)
	assert.Equal(t, mcName, requests[0].Name, "request should carry the ModelCache name from the annotation")
	assert.Empty(t, requests[0].Namespace, "request namespace should be empty for cluster-scoped ModelCache")
}

// TestPodToModelCacheRequests_WaitingForAnnotationWithNamespace verifies that a
// value containing a slash (e.g. "namespace/name") is returned verbatim — the
// function does not parse the annotation value, it passes it through directly as
// the Name field of the NamespacedName. Callers are responsible for well-formed values.
func TestPodToModelCacheRequests_AnnotationValuePassedThrough(t *testing.T) {
	const rawValue = "some-value-with-content"

	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "test-pod",
			Namespace: "default",
			Annotations: map[string]string{
				"oci-model-cache.jomcgi.dev/waiting-for": rawValue,
			},
		},
	}

	requests := podToModelCacheRequests(context.Background(), pod)
	require.Len(t, requests, 1)
	assert.Equal(t, rawValue, requests[0].Name)
}
