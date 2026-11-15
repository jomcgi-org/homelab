package store

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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
	old := Grant{Name: "one", LoginID: "old-login", LoginGeneration: 7, LoginAt: now.Add(time.Hour), LastRefresh: now.Add(time.Hour), TokenBundle: TokenBundle{RefreshToken: "old-refresh"}}
	if err := st.SaveGrant(old); err != nil {
		t.Fatal(err)
	}
	login := Grant{Name: "one", LoginID: "new-login", LoginGeneration: 8, LoginAt: now, LastRefresh: now, TokenBundle: TokenBundle{RefreshToken: "new-refresh"}}
	if err := st.SaveGrantIfNewer(login); err != nil {
		t.Fatal("an older authorization with a later refresh timestamp blocked the login")
	}
	loaded, err := st.LoadGrant("one")
	if err != nil || loaded.LoginID != login.LoginID || loaded.LoginGeneration != 8 || !loaded.LoginAt.Equal(login.LoginAt) {
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

// Model the API server's resourceVersion check; the client-go fake tracker
// does not reject stale Secret updates. Both initial GETs read revision 1.
type concurrentSecretTransport struct {
	mu       sync.Mutex
	secret   *corev1.Secret
	reads    int
	readBoth chan struct{}
}

func (s *concurrentSecretTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	var body []byte
	status := http.StatusOK
	if r.Method == http.MethodGet {
		s.mu.Lock()
		body, _ = json.Marshal(s.secret)
		s.reads++
		wait := s.reads <= 2
		if s.reads == 2 {
			close(s.readBoth)
		}
		s.mu.Unlock()
		if wait {
			select {
			case <-s.readBoth:
			case <-r.Context().Done():
				return nil, r.Context().Err()
			}
		}
	} else {
		var proposed corev1.Secret
		if err := json.NewDecoder(r.Body).Decode(&proposed); err != nil {
			return nil, err
		}
		s.mu.Lock()
		if proposed.ResourceVersion != s.secret.ResourceVersion {
			status = http.StatusConflict
			body = []byte(`{"apiVersion":"v1","kind":"Status","status":"Failure","reason":"Conflict","code":409}`)
		} else {
			proposed.ResourceVersion = "2"
			s.secret = &proposed
			body, _ = json.Marshal(s.secret)
		}
		s.mu.Unlock()
	}
	return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": []string{"application/json"}}, Body: io.NopCloser(strings.NewReader(string(body)))}, nil
}

func TestConcurrentLoginGenerationUsesSecretCAS(t *testing.T) {
	now := time.Now().UTC()
	transport := &concurrentSecretTransport{
		secret: &corev1.Secret{
			TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Secret"},
			ObjectMeta: metav1.ObjectMeta{Name: SecretName("one"), Namespace: "test", ResourceVersion: "1"},
			Data:       data(Grant{LoginID: "old", LoginGeneration: 7, LoginAt: now.Add(time.Hour), LastRefresh: now}),
		},
		readBoth: make(chan struct{}),
	}
	client, err := kubernetes.NewForConfig(&rest.Config{
		Host:          "https://kube.invalid",
		Transport:     transport,
		ContentConfig: rest.ContentConfig{ContentType: "application/json", AcceptContentTypes: "application/json"},
	})
	if err != nil {
		t.Fatal(err)
	}
	st := &SecretStore{Client: client, Namespace: "test"}
	first := Grant{Name: "one", LoginID: "first", LoginGeneration: 8, LoginAt: now, LastRefresh: now}
	second := first
	second.LoginID, second.LoginAt = "second", now.Add(-time.Hour)
	result := make(chan error, 2)
	go func() { result <- st.SaveGrantIfNewer(first) }()
	go func() { result <- st.SaveGrantIfNewer(second) }()
	successes, conflicts := 0, 0
	for range 2 {
		switch err := <-result; {
		case err == nil:
			successes++
		case apierrors.IsConflict(err):
			conflicts++
		default:
			t.Fatal("unexpected persistence result")
		}
	}
	if successes != 1 || conflicts != 1 {
		t.Fatal("concurrent logins both installed the same generation")
	}
	saved, err := st.LoadGrant("one")
	if err != nil || saved.LoginGeneration != 8 {
		t.Fatal("winning generation was not durable")
	}
	loser := first
	if saved.LoginID == first.LoginID {
		loser = second
	}
	loser.LastRefresh = now.Add(24 * time.Hour)
	if err := st.SaveGrantIfNewer(loser); !errors.Is(err, ErrGrantNotNewer) {
		t.Fatal("a losing identity replaced the durable winner of its generation")
	}
}

func TestInvalidLoginGenerationFailsClosed(t *testing.T) {
	for _, raw := range []string{"-1", "18446744073709551616", "invalid"} {
		if _, err := grantVersion(map[string][]byte{"login_generation": []byte(raw)}); err == nil {
			t.Fatal("invalid durable generation was accepted")
		}
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
