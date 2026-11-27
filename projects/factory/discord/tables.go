package discord

import (
	"encoding/json"
	"regexp"
	"strings"
	"unicode"
)

// Discord shows no Markdown tables. The bridge finds each GFM table in an
// answer and asks the renderer's /table route to draw it. A table it cannot
// draw becomes an aligned monospace table in a code block.

// markdownTable is one GFM table found in an answer.
type markdownTable struct {
	start, end int // byte offsets of the table's lines in the answer
	columns    []string
	align      []string
	rows       [][]string
}

var delimiterCell = regexp.MustCompile(`^:?-+:?$`)

// fenceRun returns the backticks or tildes that start line.
func fenceRun(line string) string {
	if line == "" || (line[0] != '`' && line[0] != '~') {
		return ""
	}
	n := 1
	for n < len(line) && line[n] == line[0] {
		n++
	}
	return line[:n]
}

// splitRow returns a table line's cells. It drops the outer pipes, splits at
// pipes that are not escaped and trims each cell.
func splitRow(line string) []string {
	line = strings.TrimSpace(line)
	line = strings.TrimPrefix(line, "|")
	if strings.HasSuffix(line, "|") && !strings.HasSuffix(line, `\|`) {
		line = line[:len(line)-1]
	}
	var cells []string
	var cell strings.Builder
	for i := 0; i < len(line); i++ {
		switch {
		case line[i] == '\\' && i+1 < len(line) && line[i+1] == '|':
			cell.WriteByte('|')
			i++
		case line[i] == '|':
			cells = append(cells, strings.TrimSpace(cell.String()))
			cell.Reset()
		default:
			cell.WriteByte(line[i])
		}
	}
	return append(cells, strings.TrimSpace(cell.String()))
}

// delimiterRow returns the column alignments of a GFM delimiter row, or nil
// when the line is not one.
func delimiterRow(line string) []string {
	if !strings.Contains(line, "-") || !strings.Contains(line, "|") {
		return nil
	}
	var align []string
	for _, cell := range splitRow(line) {
		if !delimiterCell.MatchString(cell) {
			return nil
		}
		switch {
		case strings.HasPrefix(cell, ":") && strings.HasSuffix(cell, ":"):
			align = append(align, "center")
		case strings.HasSuffix(cell, ":"):
			align = append(align, "right")
		default:
			align = append(align, "left")
		}
	}
	return align
}

// findTables returns the GFM tables in answer outside fenced code blocks, in
// answer order. A table is a header row, a delimiter row with the same
// number of cells, and the rows that follow up to a line with no pipe.
func findTables(answer string) []markdownTable {
	type line struct {
		text       string
		start, end int
	}
	var lines []line
	for at := 0; at < len(answer); {
		end := strings.IndexByte(answer[at:], '\n')
		if end < 0 {
			end = len(answer)
		} else {
			end += at + 1
		}
		lines = append(lines, line{strings.TrimRight(answer[at:end], "\r\n"), at, end})
		at = end
	}
	var tables []markdownTable
	fence := ""
	for i := 0; i < len(lines); i++ {
		trimmed := strings.TrimSpace(lines[i].text)
		if fence != "" {
			// A fence closes at a line of at least as many of its marks.
			if run := fenceRun(trimmed); len(run) >= len(fence) && run[0] == fence[0] &&
				strings.TrimSpace(trimmed[len(run):]) == "" {
				fence = ""
			}
			continue
		}
		if run := fenceRun(trimmed); len(run) >= 3 {
			fence = run
			continue
		}
		if i+1 >= len(lines) || !strings.Contains(trimmed, "|") {
			continue
		}
		columns := splitRow(trimmed)
		align := delimiterRow(lines[i+1].text)
		if align == nil || len(align) != len(columns) {
			continue
		}
		table := markdownTable{start: lines[i].start, columns: columns, align: align}
		j := i + 2
		for ; j < len(lines); j++ {
			row := strings.TrimSpace(lines[j].text)
			if row == "" || !strings.Contains(row, "|") {
				break
			}
			cells := splitRow(row)
			for len(cells) < len(columns) {
				cells = append(cells, "")
			}
			table.rows = append(table.rows, cells[:len(columns)])
		}
		table.end = lines[j-1].end
		tables = append(tables, table)
		i = j - 1
	}
	return tables
}

var (
	inlineLink = regexp.MustCompile(`\[([^\]]*)\]\([^)]*\)`)
	lineBreak  = regexp.MustCompile(`(?i)<br\s*/?>`)
	emphasis   = strings.NewReplacer("**", "", "__", "", "`", "")

	trailingSpaces = regexp.MustCompile(` +\n`)
)

// plain removes the inline Markdown a table image cannot show: emphasis,
// code marks and link targets. <br> becomes a line break.
func plain(cell string) string {
	cell = inlineLink.ReplaceAllString(cell, "$1")
	cell = lineBreak.ReplaceAllString(cell, "\n")
	return emphasis.Replace(cell)
}

// spec is the renderer's /table request for the table.
func (t markdownTable) spec() []byte {
	clean := func(cells []string) []string {
		out := make([]string, len(cells))
		for i, cell := range cells {
			out[i] = plain(cell)
		}
		return out
	}
	rows := make([][]string, len(t.rows))
	for i, row := range t.rows {
		rows[i] = clean(row)
	}
	spec, _ := json.Marshal(map[string]any{"columns": clean(t.columns), "align": t.align, "rows": rows})
	return spec
}

// displayWidth counts the monospace columns a string takes: wide
// characters and emoji take two, joiners and variation selectors none.
func displayWidth(s string) int {
	n := 0
	for _, r := range s {
		switch {
		case r == '‍' || unicode.Is(unicode.Variation_Selector, r) || unicode.Is(unicode.Mn, r):
		case r >= 0x1100 && (r <= 0x115f || (r >= 0x2e80 && r <= 0xa4cf) ||
			(r >= 0xac00 && r <= 0xd7a3) || (r >= 0xf900 && r <= 0xfaff) ||
			(r >= 0xff00 && r <= 0xff60) || (r >= 0x2600 && r <= 0x27bf) ||
			(r >= 0x2b00 && r <= 0x2bff) || (r >= 0x1f000 && r <= 0x1faff)):
			n += 2
		default:
			n++
		}
	}
	return n
}

// monospace returns the table as aligned text in a code block, for Discord
// to show when the image is not drawn.
func (t markdownTable) monospace() string {
	all := append([][]string{t.columns}, t.rows...)
	cells := make([][]string, len(all))
	widths := make([]int, len(t.columns))
	for r, row := range all {
		cells[r] = make([]string, len(row))
		for c, cell := range row {
			cell = strings.ReplaceAll(plain(cell), "\n", " ")
			cell = strings.ReplaceAll(cell, "```", "'''")
			cells[r][c] = cell
			widths[c] = max(widths[c], displayWidth(cell))
		}
	}
	var b strings.Builder
	b.WriteString("```\n")
	for r, row := range cells {
		for c, cell := range row {
			if c > 0 {
				b.WriteString(" | ")
			}
			pad := widths[c] - displayWidth(cell)
			switch t.align[c] {
			case "right":
				b.WriteString(strings.Repeat(" ", pad) + cell)
			case "center":
				b.WriteString(strings.Repeat(" ", pad/2) + cell + strings.Repeat(" ", pad-pad/2))
			default:
				b.WriteString(cell + strings.Repeat(" ", pad))
			}
		}
		b.WriteString("\n")
		if r == 0 {
			for c, w := range widths {
				if c > 0 {
					b.WriteString("-+-")
				}
				b.WriteString(strings.Repeat("-", w))
			}
			b.WriteString("\n")
		}
	}
	b.WriteString("```\n")
	// Trailing spaces would only widen the block.
	return trailingSpaces.ReplaceAllString(b.String(), "\n")
}
