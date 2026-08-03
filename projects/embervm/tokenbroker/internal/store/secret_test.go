package store

import (
	"errors"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestSecretStorePerGrant(t *testing.T) {
	s := &SecretStore{Client: fake.NewSimpleClientset(&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: SecretName("one"), Namespace: "embervm"}}, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: SecretName("two"), Namespace: "embervm"}}), Namespace: "embervm"}
	first := Grant{Name: "one", ProviderName: "provider", LastRefresh: time.Unix(10, 0).UTC(), TokenBundle: TokenBundle{AccessToken: "access", RefreshToken: "refresh"}}
	if err := s.SaveGrant(first); err != nil {
		t.Fatal(err)
	}
	if err := s.SaveGrant(Grant{Name: "two", ProviderName: "provider", LastRefresh: time.Unix(11, 0).UTC(), TokenBundle: TokenBundle{AccessToken: "other"}}); err != nil {
		t.Fatal(err)
	}
	got, err := s.LoadGrant("one")
	if err != nil || got.TokenBundle.AccessToken != "access" || got.ProviderName != "" {
		t.Fatalf("LoadGrant() = %+v, %v", got, err)
	}
	if err = s.SaveGrantIfNewer(Grant{Name: "one", LastRefresh: time.Unix(9, 0).UTC(), TokenBundle: TokenBundle{AccessToken: "stale"}}); !errors.Is(err, ErrGrantNotNewer) {
		t.Fatalf("stale SaveGrantIfNewer error = %v, want ErrGrantNotNewer", err)
	}
	got, err = s.LoadGrant("one")
	if err != nil || got.TokenBundle.AccessToken != "access" {
		t.Fatalf("stale SaveGrantIfNewer overwrote grant: %+v, %v", got, err)
	}
}
