package bootenv

import (
	"reflect"
	"testing"
)

func TestValidKey(t *testing.T) {
	for _, key := range []string{"A", "z", "_", "0", "9_LEADING", "lower_UPPER_0123456789"} {
		if !ValidKey(key) {
			t.Errorf("rejected %q", key)
		}
	}
	for _, key := range []string{"", "a-b", "a.b", "a b", "a=b", "é", "Ａ", "a\x00", "a\n"} {
		if ValidKey(key) {
			t.Errorf("accepted %q", key)
		}
	}
}

func TestEncodeWire(t *testing.T) {
	values := map[string]string{"Z": "f", "A": "\xfb\xff", "9": "hello world", "bad-key": "skip", "": "skip"}
	const want = "ember.env.9=aGVsbG8gd29ybGQ ember.env.A=-_8 ember.env.Z=Zg"
	for range 20 {
		if got := Encode(values); got != want {
			t.Fatalf("Encode = %q, want %q", got, want)
		}
	}
	if Encode(nil) != "" || Encode(map[string]string{"bad-key": "x"}) != "" {
		t.Fatal("no valid entries should encode to an empty string")
	}
}

func TestRoundTripNonemptyValues(t *testing.T) {
	values := map[string]string{"A": "synthetic secret = with spaces\n", "9_KEY": "\x00\xff", "_": "世界"}
	if got := Decode(Encode(values)); !reflect.DeepEqual(got, values) {
		t.Fatalf("round trip = %q, want %q", got, values)
	}
}

func TestEmptyValue(t *testing.T) {
	if got := Encode(map[string]string{"EMPTY": ""}); got != "ember.env.EMPTY=" {
		t.Fatalf("empty wire = %q", got)
	}
	if got := Decode("ember.env.EMPTY="); len(got) != 0 {
		t.Fatalf("empty token decoded: %q", got)
	}
}

func TestDecodeSkipsMalformedTokens(t *testing.T) {
	cmdline := "console=ttyS0\tember.env.A=b25l\nember.env.A=dHdv " +
		"ember.env.A=%%% ember.env.A= ember.env.A=YQ== " +
		"ember.env.bad-key=b25l ember.env.=b25l ember.env.é=b25l " +
		"other.env.B=b25l ember.env.B ember.env.B=Z ember.env.NUL=AA " +
		"ember.env.9=b2s\u2003ember.env._=eA"
	want := map[string]string{"A": "two", "NUL": "\x00", "9": "ok", "_": "x"}
	if got := Decode(cmdline); !reflect.DeepEqual(got, want) {
		t.Fatalf("Decode = %q, want %q", got, want)
	}
	if got := Decode(""); got == nil || len(got) != 0 {
		t.Fatalf("empty command line = %v", got)
	}
}

func TestValue(t *testing.T) {
	for _, tc := range []struct{ cmdline, key, want string }{
		{"port=1\tport=2\nport= port", "port", "2"},
		{"port.other=1 otherport=2 port=3=4 port=", "port", "3=4"},
		{"port=1", "missing", ""},
		{"=first =last", "", "last"},
		{"port=1\u2003port=2", "port", "2"},
	} {
		if got := Value(tc.cmdline, tc.key); got != tc.want {
			t.Errorf("Value(%q, %q) = %q, want %q", tc.cmdline, tc.key, got, tc.want)
		}
	}
}
