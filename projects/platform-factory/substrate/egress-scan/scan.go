package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"mime"
	"net/url"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
)

const (
	maxBody              = 512 << 10
	maxAddressAndHeaders = 64 << 10
	maxCheckMessage      = maxBody + 2*maxAddressAndHeaders
	maxScanTime          = 75 * time.Millisecond
)

type scanner struct {
	authv3.UnimplementedAuthorizationServer
	detector *requestDetector
	slots    chan struct{}
}

func (s *scanner) Check(ctx context.Context, check *authv3.CheckRequest) (*authv3.CheckResponse, error) {
	select {
	case s.slots <- struct{}{}:
		defer func() { <-s.slots }()
	default:
		return deny(codes.ResourceExhausted, typev3.StatusCode_ServiceUnavailable, "scan capacity exhausted"), nil
	}
	scanCtx, cancel := context.WithTimeout(ctx, maxScanTime)
	defer cancel()

	req := check.GetAttributes().GetRequest().GetHttp()
	if req == nil || req.GetMethod() == "" || req.GetSize() != int64(len(req.GetRawBody())) || req.GetBody() != "" {
		return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "scan requires a complete request"), nil
	}
	if len(req.GetRawBody()) > maxBody {
		return deny(codes.InvalidArgument, typev3.StatusCode_PayloadTooLarge, "request exceeds scan limit"), nil
	}

	address, err := url.ParseRequestURI(req.GetPath())
	if err != nil || !strings.HasPrefix(req.GetPath(), "/") {
		return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "invalid request target"), nil
	}
	query, err := url.ParseQuery(address.RawQuery)
	if err != nil {
		return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "invalid query"), nil
	}

	// Agentgateway supplies lower-case headers and joins repeated values.
	// Reject alternate header representations so one input is authoritative.
	if req.GetHeaderMap() != nil {
		return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "unsupported header representation"), nil
	}
	headers := req.GetHeaders()
	if count, ok := headers["content-length"]; ok {
		n, err := strconv.ParseInt(count, 10, 64)
		if err != nil || n != req.GetSize() {
			return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "scan requires a complete request"), nil
		}
	}
	encoding := strings.TrimSpace(headers["content-encoding"])
	if encoding != "" && !strings.EqualFold(encoding, "identity") {
		return deny(codes.InvalidArgument, typev3.StatusCode_UnsupportedMediaType, "unsupported content encoding"), nil
	}

	var fields strings.Builder
	fields.WriteString(req.GetHost())
	fields.WriteByte('\n')
	fields.WriteString(address.Path)
	fields.WriteByte('\n')
	for name, values := range query {
		for _, value := range values {
			fields.WriteString(name)
			fields.WriteByte('=')
			fields.WriteString(value)
			fields.WriteByte('\n')
		}
	}
	for name, value := range headers {
		fields.WriteString(name)
		fields.WriteString(": ")
		fields.WriteString(value)
		fields.WriteByte('\n')
	}
	if fields.Len() > maxAddressAndHeaders {
		return deny(codes.InvalidArgument, typev3.StatusCode_PayloadTooLarge, "request target or headers exceed scan limit"), nil
	}
	if !utf8.ValidString(fields.String()) {
		return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "invalid text in request target or headers"), nil
	}

	body := req.GetRawBody()
	var decoded string
	if len(body) != 0 {
		media, params, err := mime.ParseMediaType(headers["content-type"])
		charset := params["charset"]
		if err != nil || (charset != "" && !strings.EqualFold(charset, "utf-8")) || !utf8.Valid(body) || bytes.IndexByte(body, 0) >= 0 {
			return deny(codes.InvalidArgument, typev3.StatusCode_UnsupportedMediaType, "scan requires UTF-8 text"), nil
		}
		switch {
		case media == "text/plain":
		case media == "application/json" || (strings.HasPrefix(media, "application/") && strings.HasSuffix(media, "+json")):
			if !json.Valid(body) {
				return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "invalid JSON body"), nil
			}
			if bytes.IndexByte(body, '\\') >= 0 {
				decoded, err = decodedJSONText(scanCtx, body)
				if err != nil {
					if scanCtx.Err() != nil {
						return scanBudgetResponse(ctx)
					}
					return deny(codes.InvalidArgument, typev3.StatusCode_BadRequest, "invalid JSON body"), nil
				}
			}
		default:
			return deny(codes.InvalidArgument, typev3.StatusCode_UnsupportedMediaType, "unsupported content type"), nil
		}
	}
	matched, err := s.detector.find(scanCtx, fields.String(), string(body), decoded)
	if err != nil {
		return scanBudgetResponse(ctx)
	}
	if matched {
		return deny(codes.PermissionDenied, typev3.StatusCode_Forbidden, "request contains a credential pattern"), nil
	}
	return &authv3.CheckResponse{Status: &statuspb.Status{}}, nil
}

func scanBudgetResponse(ctx context.Context) (*authv3.CheckResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return deny(codes.ResourceExhausted, typev3.StatusCode_ServiceUnavailable, "request exceeds scan work limit"), nil
}

// Scan decoded JSON strings in addition to the original text. Tokenization
// visits each occurrence of a key, including values under duplicate keys.
func decodedJSONText(ctx context.Context, body []byte) (string, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var fields strings.Builder
	for {
		if err := ctx.Err(); err != nil {
			return "", err
		}
		token, err := dec.Token()
		if err == io.EOF {
			return fields.String(), nil
		}
		if err != nil {
			return "", err
		}
		if text, ok := token.(string); ok {
			fields.WriteString(text)
			fields.WriteByte('\n')
		}
	}
}

func deny(code codes.Code, status typev3.StatusCode, reason string) *authv3.CheckResponse {
	return &authv3.CheckResponse{
		Status: &statuspb.Status{Code: int32(code), Message: reason},
		HttpResponse: &authv3.CheckResponse_DeniedResponse{DeniedResponse: &authv3.DeniedHttpResponse{
			Status: &typev3.HttpStatus{Code: status},
			Body:   reason,
		}},
	}
}
