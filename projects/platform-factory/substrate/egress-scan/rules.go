package main

import (
	"context"
	"errors"
	"math"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/zricethezav/gitleaks/v8/config"
)

const maxSuppressedMatches = 64

var errScanBudget = errors.New("scan work limit reached")

type requestDetector struct {
	rules  []requestRule
	global config.Allowlist
}

type requestRule struct {
	config.Rule
	next *regexp.Regexp
}

func compileRequestRules(cfg config.Config) (*requestDetector, error) {
	d := &requestDetector{global: cfg.Allowlist}
	for _, id := range cfg.OrderedRules {
		rule := cfg.Rules[id]
		// Embedded rules for a specific repository file cannot inspect HTTP.
		if rule.Path != nil || rule.Regex == nil {
			continue
		}
		// After a suppressed result, search from the preceding rune. Consume
		// it so word boundaries and line anchors see the real left context,
		// and a start-of-text anchor cannot match the sliced input.
		next, err := regexp.Compile("(?s:\\A..*?)(" + rule.Regex.String() + ")")
		if err != nil {
			return nil, errors.New("cannot compile request rule")
		}
		d.rules = append(d.rules, requestRule{Rule: rule, next: next})
	}
	if len(d.rules) == 0 {
		return nil, errors.New("no request rules loaded")
	}
	return d, nil
}

func (d *requestDetector) find(ctx context.Context, fields ...string) (bool, error) {
	remaining := maxSuppressedMatches
	for _, text := range fields {
		if text == "" {
			continue
		}
		lower := strings.ToLower(text)
		for _, rule := range d.rules {
			if err := ctx.Err(); err != nil {
				return false, err
			}
			if !hasKeyword(lower, rule.Keywords) {
				continue
			}
			pos := 0
			for {
				index := rule.match(ctx, text, pos)
				if err := ctx.Err(); err != nil {
					return false, err
				}
				if index == nil {
					break
				}
				start, end := index[0], index[1]
				if start < end && !d.suppresses(ctx, rule.Rule, text, index) {
					if err := ctx.Err(); err != nil {
						return false, err
					}
					return true, nil
				}
				remaining--
				if remaining == 0 {
					return false, errScanBudget
				}
				pos = end
				if start == end {
					_, width := utf8.DecodeRuneInString(text[end:])
					if width == 0 {
						break
					}
					pos += width
				}
			}
		}
	}
	return false, ctx.Err()
}

func hasKeyword(lower string, keywords []string) bool {
	for _, keyword := range keywords {
		if strings.Contains(lower, keyword) {
			return true
		}
	}
	return len(keywords) == 0
}

func (r requestRule) match(ctx context.Context, text string, pos int) []int {
	if pos == 0 {
		return r.Regex.FindReaderSubmatchIndex(newScanReader(ctx, text))
	}
	_, width := utf8.DecodeLastRuneInString(text[:pos])
	base := pos - width
	index := r.next.FindReaderSubmatchIndex(newScanReader(ctx, text[base:]))
	if index == nil {
		return nil
	}
	index = index[2:]
	for i, at := range index {
		if at >= 0 {
			index[i] = base + at
		}
	}
	return index
}

func (d *requestDetector) suppresses(ctx context.Context, rule config.Rule, text string, index []int) bool {
	match := strings.Trim(text[index[0]:index[1]], "\n")
	secret := match
	for i := 2; i < len(index); i += 2 {
		if rule.SecretGroup == i/2 || (rule.SecretGroup == 0 && index[i] >= 0 && index[i] != index[i+1]) {
			secret = ""
			if index[i] >= 0 {
				secret = text[index[i]:index[i+1]]
			}
			break
		}
	}
	if rule.Entropy != 0 && entropy(secret) <= rule.Entropy {
		return true
	}
	line := func() string {
		start := strings.LastIndexAny(text[:index[0]], "\r\n") + 1
		end := len(text)
		if offset := strings.IndexAny(text[index[1]:], "\r\n"); offset >= 0 {
			end = index[1] + offset
		}
		return text[start:end]
	}
	if contentAllowed(ctx, d.global, match, secret, line) {
		return true
	}
	for _, allowlist := range rule.Allowlists {
		if contentAllowed(ctx, allowlist, match, secret, line) {
			return true
		}
	}
	return false
}

func contentAllowed(ctx context.Context, allowlist config.Allowlist, match, secret string, line func() string) bool {
	if ctx.Err() != nil {
		return false
	}
	and := allowlist.MatchCondition == config.AllowlistMatchAnd
	if and && (len(allowlist.Commits) > 0 || len(allowlist.Paths) > 0) {
		// HTTP has no repository path or commit.
		return false
	}
	target := secret
	switch allowlist.RegexTarget {
	case "match":
		target = match
	case "line":
		target = line()
	}
	regexAllowed := false
	if target != "" {
		for _, re := range allowlist.Regexes {
			if re.FindReaderIndex(newScanReader(ctx, target)) != nil {
				regexAllowed = true
				break
			}
		}
	}
	stopwordAllowed, _ := allowlist.ContainsStopWord(secret)
	if and && len(allowlist.Regexes) != 0 && len(allowlist.StopWords) != 0 {
		return regexAllowed && stopwordAllowed
	}
	return regexAllowed || stopwordAllowed
}

func entropy(secret string) float64 {
	counts := make(map[rune]int)
	for _, char := range secret {
		counts[char]++
	}
	var value float64
	for _, count := range counts {
		p := float64(count) / float64(len(secret))
		value -= p * math.Log2(p)
	}
	return value
}

type scanReader struct {
	context context.Context
	input   *strings.Reader
	count   uint
}

func newScanReader(ctx context.Context, input string) *scanReader {
	return &scanReader{context: ctx, input: strings.NewReader(input)}
}

func (r *scanReader) ReadRune() (rune, int, error) {
	if r.count%256 == 0 {
		if err := r.context.Err(); err != nil {
			return 0, 0, err
		}
	}
	r.count++
	return r.input.ReadRune()
}
