package provider

import (
	"context"
	"errors"
	"time"
)

var ErrRefreshTokenReused = errors.New("refresh_token_reused")

type DeviceCodeResponse struct {
	DeviceAuthID    string `json:"device_auth_id"`
	UserCode        string `json:"user_code"`
	Interval        int    `json:"interval"`
	VerificationURL string `json:"verification_url"`
	ExpiresIn       int    `json:"expires_in"`
}

type AuthorizationCodeResponse struct {
	AuthorizationCode string `json:"authorization_code"`
	CodeChallenge     string `json:"code_challenge"`
	CodeVerifier      string `json:"code_verifier"`
}

type TokenResponse struct {
	IDToken      string    `json:"id_token"`
	AccessToken  string    `json:"access_token"`
	RefreshToken string    `json:"refresh_token"`
	ExpiresIn    int       `json:"expires_in"`
	ExpiresAt    time.Time `json:"-"`
	Error        string    `json:"error"`
}

type Adapter interface {
	StartDeviceFlow(context.Context) (DeviceCodeResponse, error)
	PollForAuthorization(context.Context, DeviceCodeResponse) (AuthorizationCodeResponse, error)
	ExchangeCode(context.Context, AuthorizationCodeResponse) (TokenResponse, error)
	RefreshToken(context.Context, string) (TokenResponse, error)
}
