# RSS evidence

Retrieved 2026-11-13 without credentials. `{os,he,gr,st,ta,dg}-empty.xml`
are unchanged response bytes from
`https://www.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/{code}`.
The `weather.metoffice.gov.uk` feed URLs redirected here with HTTP 301.
The producer requests the `www` host directly and continues to reject redirects.
All six were RSS 2.0 channels with no item elements. Channel pubDate was
2026-11-10, independent of successful HTTP collection on November 13.

`se-2023.xml` is the historical RSS XML quoted by its collector on 2023-05-10:
https://community.home-assistant.io/t/another-how-do-i-parse-this-json-question/570159.
The forum masks dc:creator's email as `[email protected]` (with a nonbreaking
space); the warning title, description, link, guid and enclosure are retained.
This secondary historical capture establishes an item grammar, not current
Scottish warning coverage. Current primary feeds had no active items to save.
Tests label generated Scottish warnings synthetic. Unsupported grammar produces
unknown. No CAP fields, severity elements or validity elements are invented.

Terms checked 2026-11-13:
https://weather.metoffice.gov.uk/guides/rss and
https://www.metoffice.gov.uk/policies/tandc, Weather gadgets and RSS feeds.
Credit Met Office, keep content and links unchanged, provide direct links;
RSS can be withdrawn. Five minutes is application policy, not a licence rule.
