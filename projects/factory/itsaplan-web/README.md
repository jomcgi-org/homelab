# itsaplan web source

The factory runs the itsaplan Community web app under AGPL-3.0. Its
[corresponding upstream source](https://github.com/croffasia/itsaplan/tree/6c7c2fe2cb9d810d29c68e054de25876b47434f8)
is pinned to v1.10.0 in the root Bazel module.

Bazel applies [the Next.js security patch](next-security.patch) before
installing the web dependencies from the lockfile. [The build](build.sh)
then applies [the Agent runs patch](run-phases.patch) and packages the
standalone Next.js output. Build the image with
`bazel build //projects/factory/itsaplan-web:image`.

The run detail reads the issue's factory phases through the authenticated
itsaplan API. It shows a connector phase only for the selected run and
attempt. If the endpoint has no matching phase or the read fails, the
detail displays the native itsaplan status.
