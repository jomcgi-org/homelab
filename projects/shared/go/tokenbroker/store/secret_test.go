package store

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/rest"
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

func TestSecretStoreNamePrefix(t *testing.T) {
	if got := (&SecretStore{}).SecretName("g"); got != "embervm-oauth-grant-g" {
		t.Fatalf("default SecretName = %q", got)
	}
	client := fake.NewSimpleClientset(&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "factory-grant-g", Namespace: "factory"}})
	s := &SecretStore{Client: client, Namespace: "factory", NamePrefix: "factory-grant-"}
	if err := s.SaveGrant(Grant{Name: "g", LastRefresh: time.Unix(10, 0).UTC(), TokenBundle: TokenBundle{AccessToken: "a"}}); err != nil {
		t.Fatal(err)
	}
	got, err := s.LoadGrant("g")
	if err != nil || got.TokenBundle.AccessToken != "a" {
		t.Fatalf("LoadGrant() = %+v, %v", got, err)
	}
	if _, err := (&SecretStore{Client: client, Namespace: "factory"}).LoadGrant("g"); err == nil {
		t.Fatal("default prefix found the prefixed Secret")
	}
}

func TestSecretStoreOrdersLoginBeforeRotation(t *testing.T) {
	st := &SecretStore{Client: fake.NewSimpleClientset(&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: SecretName("one"), Namespace: "test"}}), Namespace: "test"}
	now := time.Now().UTC()
	old := Grant{Name: "one", LoginID: "old-login", LoginAt: now.Add(-time.Hour), LastRefresh: now.Add(time.Hour), TokenBundle: TokenBundle{RefreshToken: "old-refresh"}}
	if err := st.SaveGrant(old); err != nil {
		t.Fatal(err)
	}
	login := Grant{Name: "one", LoginID: "new-login", LoginAt: now, LastRefresh: now, TokenBundle: TokenBundle{RefreshToken: "new-refresh"}}
	if err := st.SaveGrantIfNewer(login); err != nil {
		t.Fatal("an older authorization with a later refresh timestamp blocked the login")
	}
	loaded, err := st.LoadGrant("one")
	if err != nil || loaded.LoginID != login.LoginID || !loaded.LoginAt.Equal(login.LoginAt) {
		t.Fatal("Secret did not retain the login identity")
	}
	old.LastRefresh = now.Add(2 * time.Hour)
	if err := st.SaveGrantIfNewer(old); !errors.Is(err, ErrGrantNotNewer) {
		t.Fatal("late old-login rotation replaced the new authorization")
	}
	rotated := loaded
	rotated.LastRefresh = now.Add(time.Second)
	rotated.TokenBundle.RefreshToken = "rotated-new-refresh"
	if err := st.SaveGrantIfNewer(rotated); err != nil {
		t.Fatal("rotation within the new login was rejected")
	}
	if err := st.SaveGrantIfNewer(login); !errors.Is(err, ErrGrantNotNewer) {
		t.Fatal("exchanged credentials replaced an already-rotated login")
	}
}

type stalledTransport struct{ operation string }

func (s stalledTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.Method == http.MethodGet && s.operation != "load" {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"apiVersion":"v1","kind":"Secret","metadata":{"name":"embervm-oauth-grant-one","namespace":"test"},"data":{}}`)),
		}, nil
	}
	<-r.Context().Done()
	return nil, r.Context().Err()
}

func TestSecretRequestsHaveDeadlines(t *testing.T) {
	for _, operation := range []string{"load", "save", "save-if-newer"} {
		t.Run(operation, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				client, err := kubernetes.NewForConfig(&rest.Config{Host: "https://kube.invalid", Transport: stalledTransport{operation: operation}})
				if err != nil {
					t.Fatal(err)
				}
				st := &SecretStore{Client: client, Namespace: "test"}
				start := time.Now()
				switch operation {
				case "load":
					_, err = st.LoadGrant("one")
				case "save":
					err = st.SaveGrant(Grant{Name: "one"})
				case "save-if-newer":
					err = st.SaveGrantIfNewer(Grant{Name: "one"})
				}
				if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) != RequestTimeout {
					t.Fatal("Secret request did not stop at its deadline")
				}
			})
		})
	}
}
