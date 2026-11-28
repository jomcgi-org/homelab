package artifacts

import (
	"fmt"
	"regexp"
	"strings"
)

// The lint warns about an HTML page that leaves the house design system, so
// the model can fix the page and put it again. It never blocks a put. It
// uses string and regular expression checks only, so it can miss a case or
// warn on one that is acceptable.

// houseLink matches a <link> to any house-N stylesheet under /_lib/.
var houseLink = regexp.MustCompile(`(?is)<link\b[^>]*\bhref\s*=\s*["']?/_lib/house-[0-9]+\.css(?:["'\s>?#]|/>)`)

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
	if found := emoji(page); len(found) > 0 {
		warnings = append(warnings, fmt.Sprintf("The page contains emoji (%s). Use Lucide icons (<i data-lucide=\"name\"></i>) or words.", strings.Join(found, " ")))
	}
	if names := rootPalette(page); len(names) > 0 {
		warnings = append(warnings, fmt.Sprintf("A :root rule sets its own colour variables (%s). Remove them and use the house palette, such as var(--bg), var(--text) and var(--accent).", strings.Join(names, ", ")))
	}
	return warnings
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
