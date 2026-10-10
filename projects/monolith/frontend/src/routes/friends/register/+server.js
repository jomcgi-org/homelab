import { error } from "@sveltejs/kit";

export function GET({ setHeaders }) {
  if (process.env.PLATFORM_AUTH_ENROLLMENT_ENABLED !== "true")
    error(404, "Registration is unavailable.");
  const target = process.env.PLATFORM_AUTH_ENROLLMENT_FLOW_URL;
  const flow = new URL(target || "https://invalid.example/");
  if (
    flow.protocol !== "https:" ||
    flow.username ||
    flow.password ||
    flow.search ||
    flow.hash ||
    flow.pathname !== "/if/flow/platform-enrollment/"
  ) {
    error(503, "Registration is unavailable.");
  }
  setHeaders({
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
  });
  // No assets or network requests carry the fragment. The code is entered on
  // Authentik's own form; credentials stay entirely inside the IdP.
  const safeTarget = flow.href
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;");
  return new Response(
    `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Join the platform</title>
<style>body{font:18px system-ui;max-width:640px;margin:4rem auto;padding:1rem}input{width:100%;padding:.7rem;font:inherit;box-sizing:border-box}button,a{display:inline-block;margin:1rem 1rem 1rem 0}</style>
<main><h1>Join the platform</h1><p>Your invitation lets you create one account. Choose a username and password in Authentik. Application access is granted separately.</p>
<section id="invitation" hidden><label>Invitation code <input id="code" readonly autocomplete="off"></label><button id="copy">Copy code</button><p>Paste this code into the invitation field, then choose your username and password. If you already have an account, sign in to Authentik first.</p><a href="${safeTarget}" rel="noreferrer">Continue to Authentik</a></section><p id="message">Open the private signup link shared by your administrator.</p></main>
<script>const raw=location.hash.slice(1);history.replaceState(null,"",location.pathname);if(/^[A-Za-z0-9_-]{43}$/.test(raw)){document.getElementById("code").value=raw;document.getElementById("invitation").hidden=false;document.getElementById("message").textContent="";}document.getElementById("copy").onclick=async()=>{try{await navigator.clipboard.writeText(document.getElementById("code").value);}catch{document.getElementById("code").select();}};window.addEventListener("pagehide",()=>{document.getElementById("code").value="";document.getElementById("invitation").hidden=true;});</script></html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}
