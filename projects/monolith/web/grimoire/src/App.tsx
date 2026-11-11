import { Route, Routes } from "react-router";
import { NotFound } from "./screens/NotFound";
import { Lobby } from "./screens/lobby/Lobby";

/** The route table. One screen per route; add a screen in `src/screens`. */
export function App() {
  return (
    <Routes>
      <Route index element={<Lobby />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
