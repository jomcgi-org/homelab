package kagent

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
)

// ListTasks retains status.message even with history and artifacts disabled.
// Project its JSON before applying the response budget. Discarded values are
// validated byte by byte, so a single large string cannot make json.Decoder
// allocate its full contents. No lifecycle evidence escapes a truncated or
// malformed response, including a malformed discarded field.
func taskMetadataJSON(body io.Reader) ([]byte, error) {
	all := &jsonFields{}
	fields := &jsonFields{object: map[string]*jsonFields{
		"error": all,
		"result": {object: map[string]*jsonFields{
			"totalSize": all, "pageSize": all, "nextPageToken": all,
			"tasks": {element: &jsonFields{object: map[string]*jsonFields{
				"id": all, "contextId": all,
				"status": {object: map[string]*jsonFields{"state": all}},
			}}},
		}},
	}}
	return projectMetadataJSON(body, fields)
}

// exactTaskMetadataJSON uses the same bounded projection for GetTask. Its
// artifacts and status message may exceed the transcript budget, but they
// cannot force a lifecycle read to scan other tasks in the session. Only fixed
// grant-failure evidence survives the status text projection.
func exactTaskMetadataJSON(body io.Reader) ([]byte, error) {
	all := &jsonFields{}
	fields := &jsonFields{object: map[string]*jsonFields{
		"error": all,
		"result": {object: map[string]*jsonFields{
			"id": all, "contextId": all,
			"status": {object: map[string]*jsonFields{
				"state": all,
				"message": {object: map[string]*jsonFields{
					"parts": {element: &jsonFields{object: map[string]*jsonFields{
						"text": {statusText: true},
					}}},
				}},
			}},
		}},
	}}
	return projectMetadataJSON(body, fields)
}

func projectMetadataJSON(body io.Reader, fields *jsonFields) ([]byte, error) {
	s := metadataReader{r: bufio.NewReader(body)}
	if err := s.value(fields, 0); err != nil {
		return nil, err
	}
	if err := s.space(); err != nil {
		return nil, err
	}
	if _, err := s.r.Peek(1); err != io.EOF {
		if err != nil {
			return nil, err
		}
		return nil, errors.New("kagent: trailing metadata JSON")
	}
	return s.out.Bytes(), nil
}

// nil discards a value; an empty rule keeps it; object/element rules select
// fields or array elements; statusText keeps only grant-failure evidence.
// Unexpected value types survive for the typed
// decoder to reject. A missing field is never synthesized.
type jsonFields struct {
	object     map[string]*jsonFields
	element    *jsonFields
	statusText bool
}

func (f *jsonFields) all() bool {
	return f != nil && f.object == nil && f.element == nil && !f.statusText
}

type metadataReader struct {
	r   *bufio.Reader
	out bytes.Buffer
}

func metadataSyntax() error { return errors.New("kagent: invalid metadata JSON") }

func (s *metadataReader) space() error {
	for {
		b, err := s.r.Peek(1)
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if b[0] != ' ' && b[0] != '\n' && b[0] != '\r' && b[0] != '\t' {
			return nil
		}
		_, _ = s.r.ReadByte()
	}
}

func (s *metadataReader) take(out *bytes.Buffer) (byte, error) {
	b, err := s.r.ReadByte()
	if err == io.EOF {
		err = io.ErrUnexpectedEOF
	}
	if err != nil {
		return 0, err
	}
	if out != nil {
		if out == &s.out && out.Len() >= maxResponseBytes {
			return 0, ErrResponseTooLarge
		}
		if out != &s.out && out.Len() >= 1024 {
			return 0, errors.New("kagent: metadata field name exceeds 1024 bytes")
		}
		out.WriteByte(b)
	}
	return b, nil
}

func (s *metadataReader) expect(want byte, out *bytes.Buffer) error {
	b, err := s.take(out)
	if err != nil {
		return err
	}
	if b != want {
		return metadataSyntax()
	}
	return nil
}

func (s *metadataReader) string(out *bytes.Buffer) error {
	if err := s.expect('"', out); err != nil {
		return err
	}
	for {
		b, err := s.take(out)
		if err != nil {
			return err
		}
		switch {
		case b == '"':
			return nil
		case b < 0x20:
			return metadataSyntax()
		case b == '\\':
			b, err = s.take(out)
			if err != nil {
				return err
			}
			switch b {
			case '"', '\\', '/', 'b', 'f', 'n', 'r', 't':
			case 'u':
				for range 4 {
					h, err := s.take(out)
					if err != nil {
						return err
					}
					if !(h >= '0' && h <= '9' || h >= 'a' && h <= 'f' || h >= 'A' && h <= 'F') {
						return metadataSyntax()
					}
				}
			default:
				return metadataSyntax()
			}
		}
	}
}

func (s *metadataReader) is(chars string) bool {
	b, err := s.r.Peek(1)
	return err == nil && strings.IndexByte(chars, b[0]) >= 0
}

func (s *metadataReader) digits(out *bytes.Buffer) error {
	if !s.is("0123456789") {
		return metadataSyntax()
	}
	for s.is("0123456789") {
		if _, err := s.take(out); err != nil {
			return err
		}
	}
	return nil
}

func (s *metadataReader) number(out *bytes.Buffer) error {
	if s.is("-") {
		if _, err := s.take(out); err != nil {
			return err
		}
	}
	if s.is("0") {
		if _, err := s.take(out); err != nil {
			return err
		}
	} else if err := s.digits(out); err != nil {
		return err
	}
	if s.is(".") {
		if _, err := s.take(out); err != nil {
			return err
		}
		if err := s.digits(out); err != nil {
			return err
		}
	}
	if s.is("eE") {
		if _, err := s.take(out); err != nil {
			return err
		}
		if s.is("+-") {
			if _, err := s.take(out); err != nil {
				return err
			}
		}
		return s.digits(out)
	}
	return nil
}

func (s *metadataReader) value(fields *jsonFields, depth int) error {
	if depth > 1000 {
		return errors.New("kagent: metadata JSON nesting limit exceeded")
	}
	if err := s.space(); err != nil {
		return err
	}
	var out *bytes.Buffer
	if fields != nil {
		out = &s.out
	}
	b, err := s.r.Peek(1)
	if err != nil {
		return err
	}
	switch b[0] {
	case '"':
		if fields != nil && fields.statusText {
			text, err := s.statusText()
			if err != nil {
				return err
			}
			data, _ := json.Marshal(text)
			if s.out.Len()+len(data) > maxResponseBytes {
				return ErrResponseTooLarge
			}
			s.out.Write(data)
			return nil
		}
		return s.string(out)
	case '{', '[':
		if depth >= 1000 {
			return errors.New("kagent: metadata JSON nesting limit exceeded")
		}
		return s.container(fields, depth, b[0], out)
	case 't', 'f', 'n':
		literal := map[byte]string{'t': "true", 'f': "false", 'n': "null"}[b[0]]
		for i := range len(literal) {
			if err := s.expect(literal[i], out); err != nil {
				return err
			}
		}
		return nil
	default:
		return s.number(out)
	}
}

func (s *metadataReader) container(fields *jsonFields, depth int, open byte, out *bytes.Buffer) error {
	close := byte(']')
	if open == '{' {
		close = '}'
	}
	if err := s.expect(open, out); err != nil {
		return err
	}
	if err := s.space(); err != nil {
		return err
	}
	emitted := false
	if !s.is(string(close)) {
		for {
			child := fields
			var key bytes.Buffer
			if open == '{' {
				var keyOut *bytes.Buffer
				if fields != nil {
					keyOut = &key
				}
				if err := s.string(keyOut); err != nil {
					return err
				}
				if fields != nil && !fields.all() {
					var name string
					if err := json.Unmarshal(key.Bytes(), &name); err != nil {
						return err
					}
					child = fields.object[name]
				}
				if err := s.space(); err != nil {
					return err
				}
				if err := s.expect(':', nil); err != nil {
					return err
				}
			} else if fields != nil && !fields.all() {
				child = fields.element
			}
			if child != nil {
				// Reserve the separator and field name before writing them.
				if s.out.Len()+key.Len()+2 > maxResponseBytes {
					return ErrResponseTooLarge
				}
				if emitted {
					s.out.WriteByte(',')
				}
				if open == '{' {
					s.out.Write(key.Bytes())
					s.out.WriteByte(':')
				}
				emitted = true
			}
			if err := s.value(child, depth+1); err != nil {
				return err
			}
			if err := s.space(); err != nil {
				return err
			}
			if s.is(string(close)) {
				break
			}
			if err := s.expect(',', nil); err != nil {
				return err
			}
			if err := s.space(); err != nil {
				return err
			}
		}
	}
	return s.expect(close, out)
}
