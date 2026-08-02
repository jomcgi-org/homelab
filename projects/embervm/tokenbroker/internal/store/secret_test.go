package store

import (
	"testing"
	"time"

	"k8s.io/client-go/kubernetes/fake"
)

func TestSecretStorePerGrant(t *testing.T) {
	s := &SecretStore{Client: fake.NewSimpleClientset(), Namespace: "embervm"}
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
	if err = s.SaveGrantIfNewer(Grant{Name: "one", LastRefresh: time.Unix(9, 0).UTC(), TokenBundle: TokenBundle{AccessToken: "stale"}}); err != nil {
		t.Fatal(err)
	}
	got, err = s.LoadGrant("one")
	if err != nil || got.TokenBundle.AccessToken != "access" {
		t.Fatalf("stale SaveGrantIfNewer overwrote grant: %+v, %v", got, err)
	}
}
