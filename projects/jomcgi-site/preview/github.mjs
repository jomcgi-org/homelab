// GitHub App access for the preview uploader: read one pull request and
// keep one comment on it. The App key stays in this process; each token it
// mints names one repository and only pull_requests: write.
import { createPrivateKey, sign } from "node:crypto";

const API = "https://api.github.com";
export const MARKER = "<!-- jomcgi-site-preview -->";

export class GitHubError extends Error {}

// env: GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY.
export function readApp(env) {
  const read = (name) => (env[name] ?? "").trim();
  const appId = read("GITHUB_APP_ID");
  const installationId = read("GITHUB_APP_INSTALLATION_ID");
  if (!/^[1-9][0-9]*$/.test(appId) || !/^[1-9][0-9]*$/.test(installationId)) {
    throw new GitHubError("invalid App identity");
  }
  const key = createPrivateKey(read("GITHUB_APP_PRIVATE_KEY"));
  if (key.asymmetricKeyType !== "rsa")
    throw new GitHubError("App key must be RSA");
  return { appId, installationId, key };
}

function appJwt({ appId, key }, now) {
  const encode = (value) =>
    Buffer.from(JSON.stringify(value)).toString("base64url");
  const unsigned = `${encode({ alg: "RS256", typ: "JWT" })}.${encode({
    iat: now - 60,
    exp: now + 540,
    iss: appId,
  })}`;
  return `${unsigned}.${sign("RSA-SHA256", Buffer.from(unsigned), key).toString("base64url")}`;
}

// app: readApp() result. repository: "owner/name".
export function gitHub({
  app,
  repository,
  fetchImpl = fetch,
  now = () => Date.now(),
}) {
  const [owner, repo] = repository.split("/");
  let cached;

  async function call(token, method, path, body) {
    const response = await fetchImpl(`${API}${path}`, {
      method,
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "User-Agent": "jomcgi-site-preview",
        "X-GitHub-Api-Version": "2022-11-28",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      redirect: "error",
      signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) {
      throw new GitHubError(
        `GitHub ${method} ${path.split("?")[0]}: HTTP ${response.status}`,
      );
    }
    return response.json();
  }

  async function token() {
    if (cached && cached.expires - now() > 5 * 60_000) return cached;
    const jwt = appJwt(app, Math.floor(now() / 1000));
    const { slug } = await call(jwt, "GET", "/app");
    const minted = await call(
      jwt,
      "POST",
      `/app/installations/${app.installationId}/access_tokens`,
      { repositories: [repo], permissions: { pull_requests: "write" } },
    );
    const permissions = Object.entries(minted.permissions ?? {})
      .sort()
      .join();
    if (
      typeof minted.token !== "string" ||
      !/^[A-Za-z0-9_.-]+$/.test(minted.token) ||
      permissions !== "metadata,read,pull_requests,write" ||
      minted.repositories?.length !== 1 ||
      minted.repositories[0].name !== repo
    ) {
      throw new GitHubError(
        "the installation token is not the requested scope",
      );
    }
    cached = {
      value: minted.token,
      login: `${slug}[bot]`,
      expires: Date.parse(minted.expires_at),
    };
    return cached;
  }

  return {
    async pull(number) {
      const { value } = await token();
      return call(value, "GET", `/repos/${owner}/${repo}/pulls/${number}`);
    },
    // Edits the App's marked comment on the PR, or creates it.
    async upsertComment(number, body) {
      const { value, login } = await token();
      for (let page = 1; page <= 30; page += 1) {
        const comments = await call(
          value,
          "GET",
          `/repos/${owner}/${repo}/issues/${number}/comments?per_page=100&page=${page}`,
        );
        const mine = comments.find(
          (c) => c.user?.login === login && c.body?.startsWith(MARKER),
        );
        if (mine) {
          await call(
            value,
            "PATCH",
            `/repos/${owner}/${repo}/issues/comments/${mine.id}`,
            { body },
          );
          return "updated";
        }
        if (comments.length < 100) break;
      }
      await call(
        value,
        "POST",
        `/repos/${owner}/${repo}/issues/${number}/comments`,
        { body },
      );
      return "created";
    },
  };
}
