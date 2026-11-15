package store

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

type TokenBundle struct {
	IDToken      string    `json:"id_token"`
	AccessToken  string    `json:"access_token"`
	RefreshToken string    `json:"refresh_token"`
	AccountID    string    `json:"account_id"`
	LastRefresh  time.Time `json:"last_refresh"`
	ExpiresAt    time.Time `json:"expires_at"`
}
type Grant struct {
	Name         string
	ProviderName string
	// LoginID and LoginAt identify an authorization across token rotations.
	// Legacy grants have neither; a new device login supersedes that lineage.
	LoginID      string
	LoginAt      time.Time
	TokenBundle  TokenBundle
	LastRefresh  time.Time
}
type Store interface {
	LoadGrant(string) (Grant, error)
	SaveGrant(Grant) error
	SaveGrantIfNewer(Grant) error
}

type SecretStore struct {
	Client    kubernetes.Interface
	Namespace string
	Context   context.Context
	// NamePrefix names each grant's Secret as NamePrefix+grant. Empty keeps
	// EmberVM's DefaultSecretPrefix.
	NamePrefix string
}

// DefaultSecretPrefix is EmberVM's grant Secret name prefix.
const DefaultSecretPrefix = "embervm-oauth-grant-"

// RequestTimeout bounds each grant read or read/update operation.
const RequestTimeout = 10 * time.Second

// CompareGrantVersion orders logins first, then rotations within a login.
// A late refresh from an older login cannot replace a newer authorization.
func CompareGrantVersion(a, b Grant) int {
	if order := a.LoginAt.Compare(b.LoginAt); order != 0 {
		return order
	}
	if order := strings.Compare(a.LoginID, b.LoginID); order != 0 {
		return order
	}
	return a.LastRefresh.Compare(b.LastRefresh)
}

func grantVersion(data map[string][]byte) (Grant, error) {
	g := Grant{LoginID: string(data["login_id"])}
	for key, dest := range map[string]*time.Time{"login_at": &g.LoginAt, "last_refresh": &g.LastRefresh} {
		if len(data[key]) == 0 {
			continue
		}
		stamp, err := time.Parse(time.RFC3339Nano, string(data[key]))
		if err != nil {
			return Grant{}, errors.New("invalid stored grant version")
		}
		*dest = stamp
	}
	if (g.LoginID == "") != g.LoginAt.IsZero() {
		return Grant{}, errors.New("incomplete stored login version")
	}
	return g, nil
}

func (s *SecretStore) ctx() context.Context {
	if s.Context != nil {
		return s.Context
	}
	return context.Background()
}

// SecretName is the grant Secret name under DefaultSecretPrefix.
func SecretName(name string) string { return DefaultSecretPrefix + name }

// SecretName is the grant Secret name under this store's prefix.
func (s *SecretStore) SecretName(name string) string {
	if s.NamePrefix == "" {
		return SecretName(name)
	}
	return s.NamePrefix + name
}

func (s *SecretStore) LoadGrant(name string) (Grant, error) {
	return s.LoadGrantContext(s.ctx(), name)
}

func (s *SecretStore) LoadGrantContext(ctx context.Context, name string) (Grant, error) {
	ctx, cancel := context.WithTimeout(ctx, RequestTimeout)
	defer cancel()
	secret, err := s.Client.CoreV1().Secrets(s.Namespace).Get(ctx, s.SecretName(name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return Grant{}, fmt.Errorf("chart-managed OAuth Secret %q not found", s.SecretName(name))
	}
	if err != nil {
		return Grant{}, err
	}
	b, err := grantVersion(secret.Data)
	if err != nil {
		return Grant{}, err
	}
	b.Name = name
	decode := func(key string) (string, error) {
		v, ok := secret.Data[key]
		if !ok {
			return "", nil
		}
		return string(v), nil
	}
	if b.TokenBundle.IDToken, err = decode("id_token"); err != nil {
		return b, err
	}
	b.TokenBundle.AccessToken, _ = decode("access_token")
	b.TokenBundle.RefreshToken, _ = decode("refresh_token")
	b.TokenBundle.AccountID, _ = decode("account_id")
	if v, _ := decode("expires_at"); v != "" {
		b.TokenBundle.ExpiresAt, err = time.Parse(time.RFC3339Nano, v)
		if err != nil {
			return b, fmt.Errorf("parse expires_at: %w", err)
		}
	}
	b.TokenBundle.LastRefresh = b.LastRefresh
	return b, nil
}

func data(g Grant) map[string][]byte {
	b := g.TokenBundle
	return map[string][]byte{
		"id_token":      []byte(b.IDToken),
		"access_token":  []byte(b.AccessToken),
		"refresh_token": []byte(b.RefreshToken),
		"account_id":    []byte(b.AccountID),
		"last_refresh":  []byte(g.LastRefresh.UTC().Format(time.RFC3339Nano)),
		"expires_at":    []byte(b.ExpiresAt.UTC().Format(time.RFC3339Nano)),
		"login_id":      []byte(g.LoginID),
		"login_at":      []byte(g.LoginAt.UTC().Format(time.RFC3339Nano)),
	}
}

func (s *SecretStore) SaveGrant(g Grant) error {
	ctx, cancel := context.WithTimeout(s.ctx(), RequestTimeout)
	defer cancel()
	secrets := s.Client.CoreV1().Secrets(s.Namespace)
	current, err := secrets.Get(ctx, s.SecretName(g.Name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return fmt.Errorf("chart-managed OAuth Secret %q not found", s.SecretName(g.Name))
	}
	if err != nil {
		return err
	}
	current.Data = data(g)
	_, err = secrets.Update(ctx, current, metav1.UpdateOptions{})
	return err
}

var ErrGrantNotNewer = errors.New("grant_not_newer")

func (s *SecretStore) SaveGrantIfNewer(g Grant) error {
	return s.SaveGrantIfNewerContext(s.ctx(), g)
}

// SaveGrantIfNewerContext bounds both API requests for a persistence attempt.
func (s *SecretStore) SaveGrantIfNewerContext(ctx context.Context, g Grant) error {
	ctx, cancel := context.WithTimeout(ctx, RequestTimeout)
	defer cancel()
	secrets := s.Client.CoreV1().Secrets(s.Namespace)
	current, err := secrets.Get(ctx, s.SecretName(g.Name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return fmt.Errorf("chart-managed OAuth Secret %q not found", s.SecretName(g.Name))
	}
	if err != nil {
		return err
	}
	previous, err := grantVersion(current.Data)
	if err != nil {
		return err
	}
	if (previous.LoginID != "" || !previous.LastRefresh.IsZero()) && CompareGrantVersion(g, previous) <= 0 {
		return ErrGrantNotNewer
	}
	current.Data = data(g)
	_, err = secrets.Update(ctx, current, metav1.UpdateOptions{})
	return err
}
