package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"time"
)

var (
	ErrInvalidGrant       = errors.New("invalid_grant")
	ErrRefreshTokenReused = errors.New("refresh_token_reused")
)

// FlexInt tolerates providers that serialize numbers as JSON strings: the
// live auth.openai.com device endpoint returns interval as "5" (observed on
// the first real login attempt), which a plain int field refuses.
type FlexInt int

func (f *FlexInt) UnmarshalJSON(data []byte) error {
	var n json.Number
	if err := json.Unmarshal(bytes.Trim(data, `"`), &n); err != nil {
		return err
	}
	v, err := n.Int64()
	if err != nil {
		return err
	}
	*f = FlexInt(v)
	return nil
}

type DeviceCodeResponse struct {
	DeviceAuthID    string  `json:"device_auth_id"`
	UserCode        string  `json:"user_code"`
	Interval        FlexInt `json:"interval"`
	VerificationURL string  `json:"verification_url"`
	ExpiresIn       FlexInt `json:"expires_in"`
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
	ExpiresIn    FlexInt   `json:"expires_in"`
	ExpiresAt    time.Time `json:"-"`
	Error        string    `json:"error"`
}

type Adapter interface {
	StartDeviceFlow(context.Context) (DeviceCodeResponse, error)
	PollForAuthorization(context.Context, DeviceCodeResponse) (AuthorizationCodeResponse, error)
	ExchangeCode(context.Context, AuthorizationCodeResponse) (TokenResponse, error)
	RefreshToken(context.Context, string) (TokenResponse, error)
}
