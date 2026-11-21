import React from "react";
import { createRoot } from "react-dom/client";
import { DiscordSDK } from "@discord/embedded-app-sdk";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import { Poll } from "./catalog.js";

const root = createRoot(document.querySelector("#root"));
async function open() {
  const sdk = new DiscordSDK(import.meta.env.VITE_DISCORD_CLIENT_ID);
  await sdk.ready();
  const { code } = await sdk.commands.authorize({
    client_id: sdk.clientId,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify", "guilds.members.read"],
  });
  const exchange = await fetch("/.proxy/activity/token", {
    method: "POST",
    body: JSON.stringify(code),
  });
  if (!exchange.ok) throw Error();
  const { access_token } = await exchange.json();
  await sdk.commands.authenticate({ access_token });
  const path =
    "/.proxy/activity/instances/" + encodeURIComponent(sdk.instanceId);
  const headers = { Authorization: "Bearer " + access_token };
  async function vote(id) {
    const response = await fetch(path, {
      headers,
      method: "POST",
      body: JSON.stringify(id),
    });
    if (!response.ok) throw Error();
  }
  function render({ components, counts }) {
    root.render(
      <Theme theme={neutralTheme}>
        <Poll
          components={components}
          counts={counts}
          vote={(id) => vote(id).catch(failed)}
        />
      </Theme>,
    );
  }
  const response = await fetch(path, {
    headers: { ...headers, Accept: "application/x-ndjson" },
  });
  if (!response.ok) throw Error();
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let pending = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) throw Error();
    pending += value;
    const lines = pending.split("\n");
    pending = lines.pop();
    for (const line of lines) render(JSON.parse(line));
  }
}
function failed() {
  root.render("This poll is unavailable. Reopen it to try again.");
}
open().catch(failed);
