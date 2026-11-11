# Platform enrollment and access

Status: backend contracts behind default-off management, enrollment and enforcement
switches. The optional Authentik platform enrollment flow was retired in #7075;
the staged backend and frontend contracts remain. Production acceptance remains on
[#6858](https://github.com/jomcgi-org/homelab/issues/6858). An implementation merge
applies the private migration but does not enable signup or management.

A platform invitation permits account creation. A campaign invitation permits
joining that campaign. Creating an account grants no application permission,
Authentik group, platform administration or campaign membership.

Authentik owns passwords, signup and sign-in. The platform owns its user
records, invitation lifecycle and application grants. Identity is the verified
issuer and subject. Joe chose username/password signup with invitation possession
on 2026-10-05. Email is optional contact information, never ownership proof;
mailbox verification and SMTP are not needed for this homelab path. Manual
credential resets remain in Authentik, outside the platform MCP interface.

**Why.** Authentik invitation creation is cross-flow and accepts arbitrary fixed
data. Joe rejected giving that authority to this integration. The retired native
flow validated a platform-owned capability and returned a signed completion proof.
The monolith holds public verification keys, not an Authentik management API
credential. Application permissions remain independent from IdP administration.

## Shared storage and commands

The private `auth.platform` module keeps the existing `auth.api` compatible.
HTTP and MCP share authorization, state changes, idempotency and audit writes.
The private module is excluded from public and agents image source closures.
Its database schema is `platform_auth`:

- Users have a stable ID, username, optional contact email and active state.
- Identities uniquely bind issuer and subject to a user; contact information
  never merges identities.
- Application links preserve Grimoire app user IDs and campaign foreign keys.
- Invitations record a recipient label, expiry, delivery/revocation state and
  the exact accepted issuer and subject. Preparation stores metadata only.
  Delivery mints a random capability, stores its digest and never extends expiry.
- Grants use the registered permission names only: `grimoire.access` and
  `grimoire.create_game`.
- Commands and audits commit with mutations. Audit bodies contain authenticated
  actor, action, target, reason and request ID, without credentials or capabilities.

The migration creates seven empty tables and their constraints/indexes. It
revokes schema/table access from `PUBLIC`, `public_reader` and `public_writer`.
It imports no identities, creates no grants and changes no existing campaign
rows. An import alone never changes application authorization.

Grimoire checks active platform status and access on protected operations when
its enforcement switch is enabled. Creating a game requires a separate grant;
the lobby reflects that grant. The application-user link is committed with
synchronization of the existing issuer/subject record, preserving memberships.
For users without email, the legacy `AppUser.email` column stores `@username` as
a login label. Registered-player invitations accept email or `@username`; in enforced mode
username lookup uses the platform account and stable application mapping, even
when contact email is present. Redemption binds to the stable app user ID.
Unbound legacy email-only rows require explicit repair in enforced mode; optional
contact email cannot claim them. Disabled mode retains the existing mailbox rule.

## Staged enrollment contract

The chart no longer supplies the optional `platform-enrollment` flow (#7075).
The staged backend endpoints and frontend registration handlers remain, behind
their enrollment switch. They do not provide a working native signup path on
their own. Ordinary Grimoire enrollment and campaign-link enrollment are separate
and remain unchanged.

The backend retains the completion contract for a maximum two-minute
`platform-registration+jwt` receipt with a dedicated audience and phase.
The monolith checks issuer, public JWKS signature, lifetime,
subject, invitation ID and digest, then locks and rechecks the invitation.
Consumption and platform account creation commit atomically. Replay binds to
the exact accepted identity. Ordinary OIDC tokens, browser success flags,
contact email and editable attributes cannot authorize completion.

The backend also retains a separate signed activation acknowledgement phase.
The retired flow supplied the IdP-side signing and activation transaction; no
replacement flow or credential provisioning is included in #7075.

Campaign links remain separate. Their capability is consumed only when the
verified recipient's Join transaction successfully creates campaign membership.
Signup and platform activation never consume a campaign link.

## Management HTTP and MCP

HTTP uses `/api/auth/platform`; the operator page is `/grimoire/platform` on the
friends host. Routes return 404 while management is disabled. The shared private
MCP server registers management tools only when enabled, without public tags.
They are absent from the public and agents tiers.

| MCP tool | Operation | Result |
| --- | --- | --- |
| `platform_operator_bootstrap` | Import this signed operator explicitly; linking its second approved issuer requires an exact target with the same stable subject | User and identity references, no default grants |
| `platform_invitation_issue` | Prepare a possession-based invitation with a recipient label, one-to-seven-day expiry, reason and idempotency key | Metadata and authenticated delivery reference |
| `platform_invitation_list` | List bounded invitation metadata | Pagination without capabilities |
| `platform_invitation_revoke` | Revoke an exact invitation | Current status |
| `platform_user_list` | List bounded platform users | Account status, identities and grants |
| `platform_user_get` | Read an exact stable user ID | Account metadata |
| `platform_user_set_active` | Enable or disable platform participation | Current state and audit reference |
| `platform_permission_list` | Read the application registry | The two Grimoire permission definitions |
| `platform_user_grant` | Grant one exact registered permission | Current state and audit reference |
| `platform_user_revoke` | Revoke one exact application grant | Current state and audit reference |

Every management command requires a standing human `operators` principal, a
protected IdP account-type claim, an approved browser or human MCP issuer, no
delegation claim and an active platform actor. Bootstrap is an explicit operation,
not automatic first-login behavior. Linking the operator's two issuers compares
the same stable subject, never email. Both live providers were verified to use
Authentik's `hashed_user_id` subject mode on 2026-10-05.

Issuing through MCP never returns a usable signup link. HTTP delivery rechecks
expiry and revocation and displays the raw link once on the authenticated operator
page. A lost response requires an authorized reissue; it rotates the digest and
invalidates the old link. Retrying preparation with the same key never mints a
capability. Normal users have a separate authenticated self page at
`/grimoire/platform/profile`, without a directory or grant mutation.

Disabling a platform account affects participating application checks. It does
not disable the Authentik account, revoke its OIDC session or change Moving.
Application grants cannot grant Authentik groups, platform management authority,
MCP access or permissions for other applications. No tool accepts a password,
Authentik bearer, OIDC client secret or arbitrary provider/group operation.

## Deployment boundary

Monolith management, enrollment and enforcement switches are independently off
by default. Authentik's management-profile switch is enabled by default; its
optional platform enrollment flow and switch were removed in #7075.
The profile switch adds protected account-type claims to Grimoire and
`mcp-friends`, without changing membership or access policy. Its single blueprint
writer is preserved for each provider. Grimoire's existing email-required
authentication gate remains unchanged.
Moving, other enrollment flows and the agents provider are unchanged.

Existing operators/users need explicit imports and application grants before
permission enforcement. Old sessions without the protected claim must sign in
again. Activation requires a reviewed deployment and real gateway/Auth/OIDC
acceptance, including new/existing players, interruptions, expiry, revoke,
concurrency, wrong identity/campaign denial and no access expansion. A native
flow render or a policy unit test is not an authenticated browser rehearsal.

The disabled implementation shipped in #6867 as monolith `0.649.9`, with
migration `20261005060000` applied and all seven platform tables empty.
The management activation stage requires private operator management and protected
identity claims. Enrollment and enforcement remain off until the operator has
explicitly imported its signed identity and received the two Grimoire grants.
A fresh Authentik login is required to obtain the new claims. Registered-player
campaign links remain enabled throughout. Subsequent signup activation and
real browser acceptance remain tracked on #6858.
