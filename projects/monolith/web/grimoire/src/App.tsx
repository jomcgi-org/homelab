import { Route, Routes } from "react-router";
import { NotFound } from "./screens/NotFound";
import { Entity } from "./screens/entity/Entity";
import { Grants } from "./screens/grants/Grants";
import { CampaignJournal } from "./screens/journal/CampaignJournal";
import { Lobby } from "./screens/lobby/Lobby";
import { Notes } from "./screens/notes/Notes";
import { Session } from "./screens/session/Session";
import { Sheets } from "./screens/sheets/Sheets";

/** The route table. One screen per route; add a screen in `src/screens`. */
export function App() {
  return (
    <Routes>
      <Route index element={<Lobby />} />
      <Route path="sheets" element={<Sheets />} />
      <Route path="campaigns/:campaignId">
        <Route path="session" element={<Session />} />
        <Route path="notes" element={<Notes />} />
        <Route path="journal" element={<CampaignJournal />} />
        <Route path="grants" element={<Grants />} />
        <Route path="entities/:entityId" element={<Entity />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
