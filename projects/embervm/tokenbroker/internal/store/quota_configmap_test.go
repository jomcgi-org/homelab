package store

import (
	"errors"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestQuotaStateStoreLoadAndCompareAndSwap(t *testing.T) {
	client := fake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "broker-quota", Namespace: "embervm", ResourceVersion: "1"},
		Data:       map[string]string{quotaStateKey: `{"version":1}`},
	})
	store := &QuotaStateStore{Client: client, Namespace: "embervm", Name: "broker-quota"}
	data, revision, err := store.Load()
	if err != nil || string(data) != `{"version":1}` || revision != "1" {
		t.Fatalf("Load() = %q, %q, %v", data, revision, err)
	}
	newRevision, err := store.CompareAndSwap(revision, []byte(`{"version":1,"providers":{},"grants":{}}`))
	if err != nil {
		t.Fatal(err)
	}
	if newRevision == "" {
		t.Fatal("CompareAndSwap returned an empty revision")
	}
	stored, _, err := store.Load()
	if err != nil || string(stored) != `{"version":1,"providers":{},"grants":{}}` {
		t.Fatalf("stored state = %q, %v", stored, err)
	}
	if _, err := store.CompareAndSwap("stale", []byte(`{}`)); !errors.Is(err, quota.ErrPersistenceConflict) {
		t.Fatalf("stale CompareAndSwap error = %v", err)
	}
}

func TestQuotaStateStoreMissingObjectIsUnavailable(t *testing.T) {
	store := &QuotaStateStore{Client: fake.NewSimpleClientset(), Namespace: "embervm", Name: "missing"}
	_, _, err := store.Load()
	if !apierrors.IsNotFound(err) {
		t.Fatalf("Load error = %v, want NotFound", err)
	}
}
