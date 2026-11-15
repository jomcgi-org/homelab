import { createPrivateKey, sign } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

// Only this init container sees the App key. Never log credentials, API bodies
// or exception details, including errors raised while parsing the private key.
try {
  const credentials = process.env.GITHUB_APP_CREDENTIALS_DIR;
  const output = process.env.RENOVATE_TOKEN_DIR;
  const username = process.env.RENOVATE_USERNAME;
  const appId = (await readFile(join(credentials, "app-id"), "utf8")).trim();
  const installationId = (
    await readFile(join(credentials, "installation-id"), "utf8")
  ).trim();
  if (
    !/^[1-9][0-9]*$/.test(appId) ||
    !/^[1-9][0-9]*$/.test(installationId) ||
    !/^[a-zA-Z0-9-]+\[bot\]$/.test(username)
  ) {
    throw new Error("Invalid App identity");
  }
  const key = createPrivateKey(
    await readFile(join(credentials, "private-key"), "utf8"),
  );
  if (key.asymmetricKeyType !== "rsa") {
    throw new Error("App key must be RSA");
  }
  const now = Math.floor(Date.now() / 1000);
  const encode = (value) =>
    Buffer.from(JSON.stringify(value)).toString("base64url");
  const unsigned = `${encode({ alg: "RS256", typ: "JWT" })}.${encode({
    iat: now - 60,
    exp: now + 540,
    iss: appId,
  })}`;
  const jwt = `${unsigned}.${sign("RSA-SHA256", Buffer.from(unsigned), key).toString("base64url")}`;
  const headers = (token) => ({
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": username,
    "X-GitHub-Api-Version": "2022-11-28",
  });
  const response = await fetch(
    `https://api.github.com/app/installations/${installationId}/access_tokens`,
    {
      method: "POST",
      headers: { ...headers(jwt), "Content-Type": "application/json" },
      body: JSON.stringify({ repositories: ["homelab"] }),
      redirect: "error",
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (response.status !== 201) {
    throw new Error("Installation token request failed");
  }
  const { token, expires_at: expiresAt } = await response.json();
  // GitHub's newer installation tokens are long and contain "." and "-".
  // Accept those, but still reject anything unsafe in a header or file.
  if (
    typeof token !== "string" ||
    !/^[A-Za-z0-9_.-]+$/.test(token) ||
    !(Date.parse(expiresAt) > Date.now() + 2_700_000)
  ) {
    throw new Error("Invalid installation token response");
  }

  // GitHub's numeric bot user ID is distinct from the App ID. Resolve it from
  // the API so the noreply author never guesses or confuses those identities.
  const userResponse = await fetch(
    `https://api.github.com/users/${encodeURIComponent(username)}`,
    {
      headers: headers(token),
      redirect: "error",
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (userResponse.status !== 200) {
    throw new Error("Bot user lookup failed");
  }
  const user = await userResponse.json();
  if (
    user.login !== username ||
    user.type !== "Bot" ||
    !Number.isSafeInteger(user.id) ||
    user.id <= 0
  ) {
    throw new Error("Invalid bot user response");
  }
  await writeFile(
    join(output, "git-author"),
    `${username} <${user.id}+${username}@users.noreply.github.com>`,
    { mode: 0o600 },
  );
  await writeFile(join(output, "token"), token, { mode: 0o600 });
} catch {
  console.error(
    "GitHub App authentication failed; check the App item and installation",
  );
  process.exitCode = 1;
}
