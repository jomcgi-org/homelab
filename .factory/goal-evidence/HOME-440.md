1. `projects/factory-landing/server.test.mjs` checks page size, order and clamping after removal.
2. `projects/factory-landing/web/src/App.test.tsx` expands a row and selects the recommended button.
3. The same UI test cancels once, confirms once, checks the POST and observes removal. `projects/factory-landing/server.test.mjs` verifies the authorized subject, origin, choice and revision at the connector boundary. `projects/factory/connector/itsaplan/decision_test.go` covers the recording endpoint. Deployment requires the verified subject described in `projects/factory-landing/README.md`.
4. `projects/factory-landing/web/src/App.test.tsx` checks direct issue links.
5. `projects/factory-landing/web/src/page.css` wraps long labels. Run `node --test projects/factory-landing/server.test.mjs` for paging and decision transport.
