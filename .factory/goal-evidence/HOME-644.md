1. projects/factory/connector/itsaplan/status.go implements thematicBreak and parseChoices. TestChoicesIgnoreCodeAndHTMLBlocks exercises a flush-left dash break after a list item.
2. parseChoices checks setextUnderline for eligible paragraph text. TestChoicesIgnoreCodeAndHTMLBlocks covers both dash and equals underlines before a standalone tag, including an underline after hash-prefixed paragraph text.
3. //projects/factory/connector/itsaplan:itsaplan_test includes status_test.go and checks each marker against the choices returned.
4. TestChoicesItemIndentation in projects/factory/connector/itsaplan/status_test.go checks nested and sibling markers against the preceding item content column. Run //projects/factory/connector/itsaplan:itsaplan_test.
