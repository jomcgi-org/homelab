import { readFileSync, writeFileSync } from "node:fs";

// Adapt the pinned v1.10.0 service while preserving the feed's wire schema.
const [source, destination] = process.argv.slice(-2);
let code = readFileSync(source, "utf8");
function replaceOnce(before, after) {
  if (code.split(before).length !== 2)
    throw new Error("itsaplan createIssue source changed");
  code = code.replace(before, after);
}

replaceOnce(
  ".returning({ id: issue.id, createdAt: issue.createdAt });\n    return row;",
  `.returning({ id: issue.id, createdAt: issue.createdAt, title: issue.title, description: issue.description });
    await recordActivityEntries([{ issueId: row.id, event: {
      action: 'created',
      subject: { value: 'factory-creation-sha256-v1' },
      from: { value: new Bun.CryptoHasher("sha256").update(row.title).digest("hex") },
      to: { value: new Bun.CryptoHasher("sha256").update(row.description).digest("hex") },
    } }], actorUserId, tx);
    return row;`,
);
replaceOnce(
  "  await recordActivity(issueId, [{ action: 'created' }], actorUserId);\n",
  "",
);
writeFileSync(destination, code);
