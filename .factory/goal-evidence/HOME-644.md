1. projects/factory/connector/itsaplan/status.go implements thematicBreak and parseChoices. TestChoicesIgnoreCodeAndHTMLBlocks exercises a flush-left dash break after a list item.
2. parseChoices checks setextUnderline for eligible paragraph text. TestChoicesIgnoreCodeAndHTMLBlocks covers both dash and equals underlines before a standalone tag.
3. //projects/factory/connector/itsaplan:itsaplan_test includes status_test.go and checks each marker against the choices returned.
