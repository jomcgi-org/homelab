import { renderToString } from "react-dom/server";
import { PAGES } from "./pages";
import { Root } from "./root";

/** The marker in each HTML entry that build.mjs replaces with the page. */
export const MARK = "<!--ssr-->";

/** A registered page's HTML, rendered in the same tree the browser hydrates. */
export function render(entry: string): string {
  const Page = PAGES[entry];
  if (!Page) throw new Error(`no page is registered for ${entry}`);
  return renderToString(
    <Root>
      <Page />
    </Root>,
  );
}
