# Review

What to check and how to rate it is in the root [`REVIEW.md`](../../REVIEW.md),
together with any `REVIEW.md` on the path to the changed files. This page is
the process.

The reviewer is independent of the author and reports findings without
editing, committing, pushing or running `ci`. Independent agent review
satisfies the review step under `AGENTS.md` "Merge authority"; routine human
sign-off is unnecessary. The dispatcher owns landing.

Review the whole finished PR diff once. Fixes require checking the changed
portion and its effects; repeat the full review only when the scope or its
assumptions changed. The dispatcher confirms that the review, its fixes and
the required checks cover the current PR head.
