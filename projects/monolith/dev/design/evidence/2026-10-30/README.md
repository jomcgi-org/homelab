# Sheet rollout evidence, 2026-10-30

Contact sheets for the technical-drawing rollout (#7016): every route on
the integration branch, captured from the dev server against a mock backend
serving the repository's own synthetic fixtures, at 1440 and 390 pixels
wide in the light and dark schemes. One row per route, left to right:
1440 light, 1440 dark, 390 light, 390 dark. Each label carries the measured
horizontal overflow, which was 0px for all 132 captures.

| Sheet | Routes |
| --- | --- |
| `public.jpg` | `/`, `/cv`, `/engineering`, `/docs`, `/blog`, `/scotland`, a 404 |
| `apps.jpg` | `/app/ships`, `/app/stars`, `/app/hikes`, `/app/trips`, `/app/notes`, `/app/grimoire`, `/app/grimoire/library`, `/app/llm-leaderboard` |
| `ember.jpg` | `/ember`, `/ember/firecracker`, `/ember/agents`, `/slop`, `/slop/factory`, `/slop/factory/activity` |
| `private.jpg` | `/`, `/review`, `/chat`, `/notes`, `/grimoire`, `/factory`, `/factory/escalations`, `/factory/execution`, `/updates` on the private host |
| `friends.jpg` | `/grimoire`, `/grimoire/sheets`, `/moving` on the friends host |

The maps use real OpenFreeMap tiles; everything else is invented fixture
data, so numbers and names are not production values. Live acceptance on
the real hosts remains on #7016.
