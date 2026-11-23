package reaper

import (
	"context"
	"errors"
	"fmt"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	core "k8s.io/client-go/kubernetes/typed/core/v1"
)

// deletionCost is the annotation a ReplicaSet reads when it removes pods: it
// removes the pod with the lowest cost first (among otherwise equal pods).
const deletionCost = "controller.kubernetes.io/pod-deletion-cost"

// ProtectBusyWorkers sets the deletion cost of each configured pool's worker
// pods: 1 for a pod that hosts an actor and 0 for an idle one. When the
// autoscaler lowers a pool's replicas, the ReplicaSet then removes idle
// workers first, so a scale-down does not kill an actor's worker.
//
// It only annotates. The cost is a hint to the ReplicaSet, and a pod that gets
// an actor between two passes (15 s) can still be chosen. A failed patch
// leaves the old cost, and the next pass tries again.
func ProtectBusyWorkers(ctx context.Context, pods core.PodsGetter, pools []Pool, busy map[Pool]map[string]struct{}) error {
	var errs []error
	for _, pool := range pools {
		list, err := pods.Pods(pool.Namespace).List(ctx, metav1.ListOptions{LabelSelector: workerPoolLabel + "=" + pool.Name})
		if err != nil {
			errs = append(errs, fmt.Errorf("list workers of %s/%s: %w", pool.Namespace, pool.Name, err))
			continue
		}
		for i := range list.Items {
			pod := &list.Items[i]
			if pod.DeletionTimestamp != nil {
				continue
			}
			want := "0"
			if _, ok := busy[pool][pod.Name]; ok {
				want = "1"
			}
			have := pod.Annotations[deletionCost]
			if have == "" {
				have = "0"
			}
			if have == want {
				continue
			}
			patch := fmt.Sprintf(`{"metadata":{"annotations":{%q:%q}}}`, deletionCost, want)
			if _, err := pods.Pods(pool.Namespace).Patch(ctx, pod.Name, types.MergePatchType, []byte(patch), metav1.PatchOptions{}); err != nil {
				errs = append(errs, fmt.Errorf("annotate %s/%s: %w", pool.Namespace, pod.Name, err))
			}
		}
	}
	return errors.Join(errs...)
}
