package kagent

import (
	"bufio"
	"io"
	"regexp"
	"strconv"
	"unicode/utf16"
	"unicode/utf8"
)

// kagent v1.0.1 reports a failed turn as status text only; the Codex error
// category stays in its traces. These patterns match whole identifiers the
// failure text carries: the broker's codex_login_required, Codex error
// categories, OpenAI error codes, and HTTP statuses written as a status.
//
// The broker's quota refusal (a 429 with no Retry-After) reaches StatusText
// as "Codex execution failed: exceeded retry limit, last status: 429 Too
// Many Requests", which quotaRE matches (HOME-127):
//   - Codex 0.161.0 does not retry a Responses 429,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/model-provider-info/src/lib.rs#L447-L452,
//     maps one without a usage-limit body to RetryLimit,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/codex-api/src/api_bridge.rs#L181-L240,
//     ends the turn without server advice to wait,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/protocol/src/error.rs#L416-L418,
//     and writes its text,
//     https://github.com/openai/codex/blob/979011409de0a60b52f179721948e65531d26144/codex-rs/protocol/src/error.rs#L668-L679.
//   - kagent v1.0.1 pins that Codex and prefixes the turn's error,
//     https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/harness/codex/internal/driver/events.go#L140-L150,
//     into the failed task's status message,
//     https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/harness/runtime/a2a/executor.go#L411-L416.
var (
	credentialRE = regexp.MustCompile(`(?i)\b(codex_login_required|invalid_api_key|unauthorized|` +
		`(http|status|status code)[ :=]*401|token_expired|refresh_token_(expired|invalid|reused))\b`)
	quotaRE = regexp.MustCompile(`(?i)\b(usage_?limit_?(reached|exceeded)|insufficient_quota|rate_limit_exceeded|` +
		`(http|status|status code)[ :=]*429)\b`)
)

// GrantFailure classifies provider grant failures using the same patterns as
// the streaming status projection. Empty means neither credential nor quota.
func GrantFailure(message string) string {
	switch {
	case credentialRE.MatchString(message):
		return "credential"
	case quotaRE.MatchString(message):
		return "quota"
	}
	return ""
}

// statusText retains only fixed failure evidence. Each regexp consumes a
// decoded stream with bounded memory, including matches after the transcript
// budget or across escapes and arbitrarily long HTTP status separators. Both
// readers drain after a match so the complete JSON string is still validated.
func (s *metadataReader) statusText() (string, error) {
	if err := s.expect('"', nil); err != nil {
		return "", err
	}
	type matcher struct {
		writer *io.PipeWriter
		result chan bool
	}
	start := func(re *regexp.Regexp) matcher {
		r, w := io.Pipe()
		result := make(chan bool, 1)
		go func() {
			defer r.Close()
			reader := bufio.NewReader(r)
			matched := re.MatchReader(reader)
			_, _ = io.Copy(io.Discard, reader)
			result <- matched
		}()
		return matcher{w, result}
	}
	credential, quota := start(credentialRE), start(quotaRE)
	_, err := io.Copy(io.MultiWriter(credential.writer, quota.writer), &statusString{r: s.r})
	_ = credential.writer.CloseWithError(err)
	_ = quota.writer.CloseWithError(err)
	credentialMatch, quotaMatch := <-credential.result, <-quota.result
	if err != nil {
		return "", err
	}
	switch {
	case credentialMatch:
		return "unauthorized (bounded credential failure summary)", nil
	case quotaMatch:
		return "usage_limit_reached (bounded quota failure summary)", nil
	}
	return "", nil
}

// statusString decodes one JSON string incrementally. It ends only at its
// closing quote, never at a truncated response. Like encoding/json, invalid
// UTF-8 and unpaired UTF-16 surrogates become the Unicode replacement rune.
type statusString struct {
	r       *bufio.Reader
	pending []byte
	buf     [utf8.UTFMax]byte
	done    bool
}

func (s *statusString) Read(p []byte) (int, error) {
	n := 0
	for n < len(p) {
		if len(s.pending) != 0 {
			copied := copy(p[n:], s.pending)
			n += copied
			s.pending = s.pending[copied:]
			continue
		}
		if s.done {
			return n, io.EOF
		}
		r, err := s.rune()
		if err != nil {
			return n, err
		}
		if !s.done {
			s.pending = s.buf[:utf8.EncodeRune(s.buf[:], r)]
		}
	}
	return n, nil
}

func (s *statusString) rune() (rune, error) {
	r, _, err := s.r.ReadRune()
	if err == io.EOF {
		err = io.ErrUnexpectedEOF
	}
	if err != nil {
		return 0, err
	}
	switch {
	case r == '"':
		s.done = true
		return 0, nil
	case r < 0x20:
		return 0, metadataSyntax()
	case r != '\\':
		return r, nil
	}
	b, err := s.r.ReadByte()
	if err == io.EOF {
		err = io.ErrUnexpectedEOF
	}
	if err != nil {
		return 0, err
	}
	switch b {
	case '"', '\\', '/':
		return rune(b), nil
	case 'b':
		return '\b', nil
	case 'f':
		return '\f', nil
	case 'n':
		return '\n', nil
	case 'r':
		return '\r', nil
	case 't':
		return '\t', nil
	case 'u':
		var hex [4]byte
		if _, err := io.ReadFull(s.r, hex[:]); err != nil {
			if err == io.EOF {
				err = io.ErrUnexpectedEOF
			}
			return 0, err
		}
		value, err := strconv.ParseUint(string(hex[:]), 16, 16)
		if err != nil {
			return 0, metadataSyntax()
		}
		r := rune(value)
		if !utf16.IsSurrogate(r) {
			return r, nil
		}
		if next, err := s.r.Peek(6); err == nil && next[0] == '\\' && next[1] == 'u' {
			low, err := strconv.ParseUint(string(next[2:]), 16, 16)
			if err == nil {
				if pair := utf16.DecodeRune(r, rune(low)); pair != utf8.RuneError {
					_, _ = s.r.Discard(6)
					return pair, nil
				}
			}
		}
		return utf8.RuneError, nil
	}
	return 0, metadataSyntax()
}
