import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { ChunkBoundary } from "./app/ChunkBoundary";
import { Page } from "./app/Page";
import { NotFound } from "./screens/NotFound";
import { Lobby } from "./screens/lobby/Lobby";

// Each screen is its own chunk, so the lobby does not download the table.
const Session = lazy(() =>
  import("./screens/session/Session").then((m) => ({ default: m.Session })),
);
const Sheets = lazy(() =>
  import("./screens/sheets/Sheets").then((m) => ({ default: m.Sheets })),
);
const Notes = lazy(() =>
  import("./screens/notes/Notes").then((m) => ({ default: m.Notes })),
);
const CampaignJournal = lazy(() =>
  import("./screens/journal/CampaignJournal").then((m) => ({
    default: m.CampaignJournal,
  })),
);
const Grants = lazy(() =>
  import("./screens/grants/Grants").then((m) => ({ default: m.Grants })),
);
const Entity = lazy(() =>
  import("./screens/entity/Entity").then((m) => ({ default: m.Entity })),
);

const JoinAccept = lazy(() =>
  import("./screens/join/JoinAccept").then((m) => ({ default: m.JoinAccept })),
);
const Platform = lazy(() =>
  import("./screens/platform/Platform").then((m) => ({ default: m.Platform })),
);
const Profile = lazy(() =>
  import("./screens/platform/Profile").then((m) => ({ default: m.Profile })),
);

function Loading() {
  return (
    <Page title="Grimoire">
      <Skeleton width="60%" height={24} />
    </Page>
  );
}

/** The route table. One screen per route; add a screen in `src/screens`. */
export function App() {
  return (
    <ChunkBoundary>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route index element={<Lobby />} />
          <Route path="sheets" element={<Sheets />} />
          <Route path="join/accept" element={<JoinAccept />} />
          <Route path="platform" element={<Platform />} />
          <Route path="platform/profile" element={<Profile />} />
          <Route path="campaigns/:campaignId">
            <Route path="session" element={<Session />} />
            <Route path="notes" element={<Notes />} />
            <Route path="journal" element={<CampaignJournal />} />
            <Route path="grants" element={<Grants />} />
            <Route path="entities/:entityId" element={<Entity />} />
          </Route>
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </ChunkBoundary>
  );
}
