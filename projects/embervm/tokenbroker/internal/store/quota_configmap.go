package store

import (
	"context"
	"fmt"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const quotaStateKey = "state.json"

// QuotaStateStore keeps non-secret quota observations in a dedicated,
// chart-created ConfigMap. Resource-version updates provide the atomic CAS used
// by quota.Store; this object never shares data with OAuth grant Secrets.
type QuotaStateStore struct {
	Client    kubernetes.Interface
	Namespace string
	Name      string
	Context   context.Context
}

func (s *QuotaStateStore) ctx() context.Context {
	if s.Context != nil {
		return s.Context
	}
	return context.Background()
}

// Load returns a missing data key as an empty snapshot. A missing ConfigMap is
// an availability failure because the chart owns object creation and RBAC does
// not permit the broker to create arbitrary ConfigMaps.
func (s *QuotaStateStore) Load() ([]byte, string, error) {
	configMap, err := s.Client.CoreV1().ConfigMaps(s.Namespace).Get(s.ctx(), s.Name, metav1.GetOptions{})
	if err != nil {
		return nil, "", err
	}
	return []byte(configMap.Data[quotaStateKey]), configMap.ResourceVersion, nil
}

// CompareAndSwap replaces the dedicated state key only when resourceVersion
// still matches the revision returned by Load.
func (s *QuotaStateStore) CompareAndSwap(revision string, data []byte) (string, error) {
	configMaps := s.Client.CoreV1().ConfigMaps(s.Namespace)
	configMap, err := configMaps.Get(s.ctx(), s.Name, metav1.GetOptions{})
	if err != nil {
		return "", err
	}
	if configMap.ResourceVersion != revision {
		return "", quota.ErrPersistenceConflict
	}
	if configMap.Data == nil {
		configMap.Data = make(map[string]string)
	}
	configMap.Data[quotaStateKey] = string(data)
	updated, err := configMaps.Update(s.ctx(), configMap, metav1.UpdateOptions{})
	if apierrors.IsConflict(err) {
		return "", quota.ErrPersistenceConflict
	}
	if err != nil {
		return "", fmt.Errorf("update quota ConfigMap %q: %w", s.Name, err)
	}
	return updated.ResourceVersion, nil
}
