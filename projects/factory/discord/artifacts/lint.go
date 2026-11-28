package artifacts

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// The lint warns about an HTML page that leaves the house design system, so
// the model can fix the page and put it again. It never blocks a put. It
// uses string and regular expression checks only, so it can miss a case or
// warn on one that is acceptable.

// houseLink matches a <link> to any house-N stylesheet under /_lib/.
var houseLink = regexp.MustCompile(`(?is)<link\b[^>]*\bhref\s*=\s*["']?/_lib/house-[0-9]+\.css(?:["'\s>?#]|/>)`)

// houseRef matches a /_lib/house-N.css or house-N.js reference and captures N.
var houseRef = regexp.MustCompile(`/_lib/house-([0-9]+)\.(?:css|js)\b`)

// newestHouse is the house version the template tool's page links. A test
// checks that it is the newest house-N in projects/factory/discord/libs.
var newestHouse = houseVersion(pageTemplate)

// houseVersion returns the highest house-N version a page references, or 0.
func houseVersion(page string) int {
	newest := 0
	for _, m := range houseRef.FindAllStringSubmatch(page, -1) {
		if n, err := strconv.Atoi(m[1]); err == nil && n > newest {
			newest = n
		}
	}
	return newest
}

// rootRule matches the body of each CSS rule whose selector names :root.
var rootRule = regexp.MustCompile(`(?s):root\b[^{}]*\{([^{}]*)\}`)

// customProp matches one custom property declaration in a rule body.
var customProp = regexp.MustCompile(`(--[A-Za-z0-9_-]+)\s*:\s*([^;]*)`)

// paletteName matches a custom property name for a background, text or
// accent colour, such as --bg, --text, --accent or --card-background.
var paletteName = regexp.MustCompile(`(?i)^--(?:[a-z0-9_-]*-)?(?:bg|background|text|fg|foreground|accent|primary|secondary|surface|ink|paper|muted|border|colou?r)(?:-[a-z0-9_-]*)?$`)

// colourValue matches a value that is a colour literal.
var colourValue = regexp.MustCompile(`(?i)^\s*(?:#[0-9a-f]{3,8}\b|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark)\s*\()`)

// sizeSuffix marks a palette-like name that holds a size, not a colour.
var sizeSuffix = regexp.MustCompile(`(?i)-(?:size|width|weight|height|radius|gap|font|family|line|leading|spacing)$`)

// maxNamed is how many offending items a warning names.
const maxNamed = 5

// lintHTML returns the warnings for one HTML page.
func lintHTML(page string) []string {
	var warnings []string
	if !houseLink.MatchString(page) {
		warnings = append(warnings, `The page does not link the house stylesheet. Keep the <link rel="stylesheet" href="/_lib/house-N.css"> tag from the template tool's page.`)
	}
	if old := staleHouse(page); len(old) > 0 {
		warnings = append(warnings, fmt.Sprintf("The page links an old house version (%s); the newest is house-%d. Call the template tool and copy its link and script tags exactly.", strings.Join(old, ", "), newestHouse))
	}
	if found := emoji(page); len(found) > 0 {
		warnings = append(warnings, fmt.Sprintf("The page contains emoji (%s). Use Lucide icons (<i data-lucide=\"name\"></i>) or words.", strings.Join(found, " ")))
	}
	if names := rootPalette(page); len(names) > 0 {
		warnings = append(warnings, fmt.Sprintf("A :root rule sets its own colour variables (%s). Remove them and use the house palette, such as var(--bg), var(--text) and var(--accent).", strings.Join(names, ", ")))
	}
	if found := colourLiterals(page); len(found) > 0 {
		warnings = append(warnings, fmt.Sprintf("The page sets its own colours (%s). Remove them: the house files colour the page, diagrams and charts. In CSS use a house token with no fallback, such as var(--muted); in a chart set no color, and mark one bar with opacity.", strings.Join(found, ", ")))
	}
	return warnings
}

// staleHouse returns the distinct house files older than newestHouse that
// the page references.
func staleHouse(page string) []string {
	var old []string
	seen := map[string]bool{}
	for _, m := range houseRef.FindAllStringSubmatch(page, -1) {
		n, err := strconv.Atoi(m[1])
		name := strings.TrimPrefix(m[0], "/_lib/")
		if err != nil || n >= newestHouse || seen[name] {
			continue
		}
		seen[name] = true
		old = append(old, name)
	}
	return old
}

// emoji returns up to maxNamed distinct emoji in the page.
func emoji(page string) []string {
	var found []string
	seen := map[rune]bool{}
	for _, r := range page {
		if !isEmoji(r) || seen[r] {
			continue
		}
		seen[r] = true
		found = append(found, string(r))
		if len(found) == maxNamed {
			break
		}
	}
	return found
}

// isEmoji reports whether r is in a block of emoji or pictographs.
func isEmoji(r rune) bool {
	switch {
	case r >= 0x1F000 && r <= 0x1FAFF: // pictographs, emoticons, flags, symbols
		return true
	case r >= 0x2600 && r <= 0x27BF: // miscellaneous symbols and dingbats
		return true
	case r == 0x2B50 || r == 0x2B55 || (r >= 0x2B05 && r <= 0x2B07) || r == 0x2B1B || r == 0x2B1C: // stars, circles, arrows
		return true
	case r == 0x231A || r == 0x231B || r == 0x23F0 || r == 0x23F3 || (r >= 0x23E9 && r <= 0x23EC): // watch, hourglass, media
		return true
	}
	return false
}

// rootPalette returns up to maxNamed colour variables that :root rules set.
func rootPalette(page string) []string {
	var names []string
	seen := map[string]bool{}
	for _, rule := range rootRule.FindAllStringSubmatch(page, -1) {
		for _, decl := range customProp.FindAllStringSubmatch(rule[1], -1) {
			name, value := decl[1], decl[2]
			palette := paletteName.MatchString(name) && !sizeSuffix.MatchString(name)
			if (!palette && !colourValue.MatchString(value)) || seen[name] {
				continue
			}
			seen[name] = true
			names = append(names, name)
			if len(names) == maxNamed {
				return names
			}
		}
	}
	return names
}

// The colour check reads the places a page sets colours: style blocks, style
// attributes, SVG paint attributes, Mermaid style lines and JSON chart specs.
// It reads the page only; the house files under /_lib/ never pass through it.
var (
	styleBlock  = regexp.MustCompile(`(?is)<style\b[^>]*>(.*?)</style>`)
	jsonScript  = regexp.MustCompile(`(?is)<script\b[^>]*\btype\s*=\s*["']?application/json\b[^>]*>(.*?)</script>`)
	mermaidPre  = regexp.MustCompile(`(?is)<pre\b[^>]*\bclass\s*=\s*["'][^"']*\bmermaid\b[^>]*>(.*?)</pre>`)
	mermaidLine = regexp.MustCompile(`(?im)^\s*(?:style|classDef|linkStyle)\b.*$`)
	// colourAttr matches a style attribute or an SVG or HTML paint attribute.
	colourAttr = regexp.MustCompile(`(?is)\s(style|fill|stroke|stop-color|flood-color|lighting-color|color|bgcolor)\s*=\s*(?:"([^"]*)"|'([^']*)')`)
	cssRule    = regexp.MustCompile(`([^{}]*)\{([^{}]*)\}`)
	cssDecl    = regexp.MustCompile(`([A-Za-z-][A-Za-z0-9_-]*)\s*:\s*([^;]*)`)
	cssURL     = regexp.MustCompile(`(?i)url\([^)]*\)`)
	// colourLiteral matches a hex colour or the start of an rgb()-style
	// colour function.
	colourLiteral = regexp.MustCompile(`(?i)#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(`)
	// wholeColour matches a value that is one colour literal.
	wholeColour = regexp.MustCompile(`(?i)^\s*(?:#(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(.*\))\s*$`)
)

// colourLiterals returns up to maxNamed distinct colour literals the page
// sets, in a stable order.
func colourLiterals(page string) []string {
	var found []string
	seen := map[string]bool{}
	add := func(lit string) {
		lit = strings.TrimSpace(lit)
		if strings.HasSuffix(lit, "(") {
			lit += "…)"
		}
		key := strings.ToLower(lit)
		if seen[key] || len(found) == maxNamed {
			return
		}
		seen[key] = true
		found = append(found, lit)
	}
	scan := func(text string) {
		for _, lit := range colourLiteral.FindAllString(cssURL.ReplaceAllString(text, ""), -1) {
			add(lit)
		}
	}
	for _, block := range styleBlock.FindAllStringSubmatch(page, -1) {
		for _, rule := range cssRule.FindAllStringSubmatch(block[1], -1) {
			root := rootRule.MatchString(rule[0])
			for _, decl := range cssDecl.FindAllStringSubmatch(rule[2], -1) {
				// The private palette warning already names :root variables.
				if root && strings.HasPrefix(decl[1], "--") {
					continue
				}
				scan(decl[2])
			}
		}
	}
	for _, attr := range colourAttr.FindAllStringSubmatch(page, -1) {
		value := attr[2] + attr[3]
		if !strings.EqualFold(attr[1], "style") {
			if wholeColour.MatchString(value) {
				add(value)
			}
			continue
		}
		for _, decl := range cssDecl.FindAllStringSubmatch(value, -1) {
			scan(decl[2])
		}
	}
	for _, pre := range mermaidPre.FindAllStringSubmatch(page, -1) {
		for _, line := range mermaidLine.FindAllString(pre[1], -1) {
			scan(line)
		}
	}
	for _, script := range jsonScript.FindAllStringSubmatch(page, -1) {
		var spec any
		if json.Unmarshal([]byte(script[1]), &spec) == nil {
			specColours(spec, add)
		}
	}
	return found
}

// specColours calls add for each string in a chart spec that is a colour
// literal. It skips inline data, where a value such as "#123" is a label.
func specColours(v any, add func(string)) {
	switch v := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			if k != "data" && k != "datasets" && k != "values" {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		for _, k := range keys {
			specColours(v[k], add)
		}
	case []any:
		for _, child := range v {
			specColours(child, add)
		}
	case string:
		if wholeColour.MatchString(v) {
			add(v)
		}
	}
}
