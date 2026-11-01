"""Name-normalisation patterns shared by extraction and alias review."""

import re

# A map-key / catalog prefix on a heading-derived name: an optional 1-2 letter
# area code, digits, an optional trailing letter, then "." or ":" and a space
# (e.g. "L17. ", "P2. ", "17. ", "A1: "). Stripped so "L17. Surgery" dedupes with
# "Surgery" (spec #3 map-key defect). Conservative: it must have a digit and a
# delimiter+space, so real names ("St. Cuthbert") are left alone.
MAP_KEY_PREFIX_RE = re.compile(r"^[A-Za-z]{0,2}\d+[A-Za-z]?[.:]\s+")
LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
# Curly quotes / apostrophes -> straight, so "Uk’otoa" dedupes with "Uk'otoa".
QUOTE_NORMALIZE = {"‘": "'", "’": "'", "“": '"', "”": '"'}
