import { randomBytes } from "node:crypto";
import { json } from "@sveltejs/kit";
import theme from "$lib/grimoire/theme.css?raw";
import { startJoinLanding } from "$lib/grimoire/join-landing.js";
import {
  JOIN_COOKIE,
  JoinLinkError,
  JOIN_UNAVAILABLE,
  bearerJoinJson,
  clearJoinToken,
  checkJoinSelection,
  enrollmentUrl,
  joinMetadata,
  joinToken,
  linksEnabled,
  saveJoinToken,
  safeJoinMessage,
} from "$lib/server/grimoire-join-links.js";

// Keep native form POST Origin intact while suppressing cross-origin referrers.
// no-referrer serializes navigation POST Origin to null, which the strict CSRF
// check below correctly rejects. Keep the response header and HTML meta aligned.
// https://fetch.spec.whatwg.org/#append-a-request-origin-header
const privateHeaders = {
  "cache-control": "private, no-store",
  "cloudflare-cdn-cache-control": "no-store",
  "referrer-policy": "same-origin",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
};
const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );

function landing(message, enabled, status = 200, signIn = false) {
  const nonce = randomBytes(18).toString("base64");
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="same-origin"><meta name="robots" content="noindex,nofollow"><title>Campaign invitation · Grimoire</title><style nonce="${nonce}">${theme}
body { margin:0; font:1rem/1.6 system-ui,sans-serif; background:var(--grim-paper); color:var(--grim-ink); }
main { max-width:40rem; margin:8vh auto; padding:1.5rem; }
h1 { font-family:var(--grim-serif); line-height:1.15; font-size:2.4rem; }
dt { font-weight:600; } dd { margin:0 0 1rem; overflow-wrap:anywhere; }
a { color:var(--grim-accent); } button,.button { display:inline-block; font:inherit; padding:.7rem 1rem; border:1px solid var(--grim-accent); border-radius:.3rem; background:var(--grim-accent); color:var(--grim-on-accent); cursor:pointer; }
form { margin:1rem 0; } button:disabled { opacity:.65; cursor:wait; } [hidden] { display:none !important; }
</style></head><body class="grimoire"><main><p>Grimoire</p><h1>Join a campaign</h1><p id="status" role="status">${escapeHtml(message)}</p><section id="invitation" aria-label="Invitation details" hidden><h2 id="campaign"></h2><dl><dt>Invited email</dt><dd id="recipient"></dd><dt>Expires</dt><dd id="expires"></dd><dt>Status</dt><dd id="link-status"></dd></dl></section><div id="actions" hidden><a id="sign-in" class="button" href="/grimoire/join/accept">Sign in to accept</a><form id="enrollment" method="POST" action="/grimoire/join" hidden><input type="hidden" name="action" value="enroll"><input id="enrollment-invitation" type="hidden" name="invitation_id"><p>New to Grimoire? Create an account with the invited email, then return here to accept.</p><button>Create account</button></form></div><form method="POST" action="/grimoire/join"><input type="hidden" name="action" value="cancel"><button>Close invitation</button></form><noscript><p>JavaScript is needed to open the private part of this link. Enable it, then open the original invitation again.</p></noscript>${signIn ? '<p><a href="/grimoire/join/accept">Sign in to continue</a></p>' : ""}<p><a href="/grimoire/join">Return to invitation</a></p></main>${enabled ? `<script nonce="${nonce}">(${startJoinLanding.toString()})();</script>` : `<script nonce="${nonce}">history.replaceState(null,"",location.pathname);</script>`}</body></html>`,
    {
      status,
      headers: {
        ...privateHeaders,
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; form-action 'self' https://auth.jomcgi.dev; base-uri 'none'; frame-ancestors 'none'`,
      },
    },
  );
}

export function GET() {
  return linksEnabled()
    ? landing("Opening your invitation…", true)
    : landing(JOIN_UNAVAILABLE, false, 503);
}

export async function POST({ request, url, fetch, cookies }) {
  const isJson = request.headers
    .get("content-type")
    ?.startsWith("application/json");
  const failure = (message, status, signIn = false) =>
    isJson
      ? json({ error: message }, { status, headers: privateHeaders })
      : landing(message, false, status, signIn);
  if (request.headers.get("origin") !== url.origin)
    return failure("Open this invitation from the original link.", 403);
  if (Number(request.headers.get("content-length") || 0) > 2048)
    return failure("Invalid invitation request.", 413);
  let data;
  try {
    const body = await request.text();
    if (body.length > 2048) return failure("Invalid invitation request.", 413);
    data = isJson
      ? JSON.parse(body)
      : Object.fromEntries(new URLSearchParams(body));
  } catch {
    return failure("Invalid invitation request.", 400);
  }
  if (data?.action === "cancel") {
    clearJoinToken(cookies);
    return landing(
      "Invitation closed. Reopen the original link when you are ready.",
      false,
    );
  }
  if (!linksEnabled()) return failure(JOIN_UNAVAILABLE, 503);
  try {
    if (data?.action === "capture" || data?.action === "inspect") {
      // New links always replace stale resumptions, including an invalid link.
      if (data.action === "capture") clearJoinToken(cookies);
      const token = joinToken(
        data.action === "capture" ? data.token : cookies.get(JOIN_COOKIE),
      );
      const metadata = joinMetadata(
        await bearerJoinJson(fetch, "inspect", token),
      );
      if (metadata.status === "pending") saveJoinToken(cookies, token);
      else clearJoinToken(cookies);
      return json(metadata, { headers: privateHeaders });
    }
    if (data?.action === "enroll") {
      const token = joinToken(cookies.get(JOIN_COOKIE));
      await checkJoinSelection(fetch, token, data.invitation_id);
      const result = await bearerJoinJson(fetch, "enroll", token);
      return new Response(null, {
        status: 303,
        headers: {
          ...privateHeaders,
          location: enrollmentUrl(result.enrollment_url),
        },
      });
    }
    return failure("Invalid invitation action.", 400);
  } catch (error) {
    return failure(
      safeJoinMessage(error),
      400,
      error instanceof JoinLinkError && error.signIn,
    );
  }
}
