package store

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
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
}
type Grant struct {
	Name         string
	ProviderName string
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
}

func (s *SecretStore) ctx() context.Context {
	if s.Context != nil {
		return s.Context
	}
	return context.Background()
}
func SecretName(name string) string { return "embervm-oauth-grant-" + name }

func (s *SecretStore) LoadGrant(name string) (Grant, error) {
	secret, err := s.Client.CoreV1().Secrets(s.Namespace).Get(s.ctx(), SecretName(name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return Grant{Name: name}, nil
	}
	if err != nil {
		return Grant{}, err
	}
	b := Grant{Name: name}
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
	if v, _ := decode("last_refresh"); v != "" {
		b.LastRefresh, err = time.Parse(time.RFC3339Nano, v)
		if err != nil {
			return b, fmt.Errorf("parse last_refresh: %w", err)
		}
	}
	b.TokenBundle.LastRefresh = b.LastRefresh
	return b, nil
}

func data(b TokenBundle, lastRefresh time.Time) map[string][]byte {
	return map[string][]byte{"id_token": []byte(b.IDToken), "access_token": []byte(b.AccessToken), "refresh_token": []byte(b.RefreshToken), "account_id": []byte(b.AccountID), "last_refresh": []byte(lastRefresh.UTC().Format(time.RFC3339Nano))}
}

func (s *SecretStore) SaveGrant(g Grant) error {
	secrets := s.Client.CoreV1().Secrets(s.Namespace)
	current, err := secrets.Get(s.ctx(), SecretName(g.Name), metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		_, err = secrets.Create(s.ctx(), &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: SecretName(g.Name)}, Data: data(g.TokenBundle, g.LastRefresh)}, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	current.Data = data(g.TokenBundle, g.LastRefresh)
	_, err = secrets.Update(s.ctx(), current, metav1.UpdateOptions{})
	return err
}

func (s *SecretStore) SaveGrantIfNewer(g Grant) error {
	current, err := s.LoadGrant(g.Name)
	if err != nil {
		return err
	}
	if !current.LastRefresh.IsZero() && !g.LastRefresh.After(current.LastRefresh) {
		return nil
	}
	return s.SaveGrant(g)
}
