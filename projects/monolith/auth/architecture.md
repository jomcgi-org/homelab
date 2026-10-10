# Platform enrollment and access

Status: implementation behind default-off management, enrollment and enforcement
switches. Production acceptance remains on
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
data. Joe rejected giving that authority to this integration. The native flow
validates a platform-owned capability and returns a signed completion proof.
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

## Invitation-required native enrollment

Platform invitations are issued by an active, explicitly bootstrapped standing
human operator. `/register#<capability>` is a self-contained landing page. It
removes the fragment from history and lets the player copy the code into the
native Authentik form. No signup capability is sent in an Authentik query string.
The player chooses a username and password inside Authentik.

The separate `platform-enrollment` flow validates the capability before writing
an inactive external user. It fixes the new account's path to that invitation,
discards arbitrary prompt fields and assigns no groups. An interrupted signup
can resume only the inactive account created for that invitation. An active
existing account must already be signed in; signup never overwrites its password.

The completion policy signs a maximum two-minute `platform-registration+jwt`
receipt with a dedicated audience and phase using the existing provider signing
key inside the IdP. The monolith checks issuer, public JWKS signature, lifetime,
subject, invitation ID and digest, then locks and rechecks the invitation.
Consumption and platform account creation commit atomically. Replay binds to
the exact accepted identity. Ordinary OIDC tokens, browser success flags,
contact email and editable attributes cannot authorize completion.

IdP activation is acknowledged with a separate signed phase. Activating a new
IdP account and sending that acknowledgement run inside an Authentik database
transaction. A completed signup link cannot reactivate an account an administrator
later disables. If the platform commits an acknowledgement but its response is
lost, the IdP transaction fails closed; manual Authentik repair may be needed.
This is not a distributed transaction. Revocation racing after native account
creation, or competing signup attempts, can also leave inactive accounts for
manual cleanup. They receive no platform access.

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
by default. Authentik's management-profile and enrollment-flow switches are also
off. The profile switch adds protected account-type claims to Grimoire and
`mcp-friends`, without changing membership or access policy. Its single blueprint
writer is preserved for each provider. Enrollment additionally mounts the
separate native flow and admits username-only human authentication to Grimoire.
Moving, other enrollment flows and the agents provider are unchanged.

Existing operators/users need explicit imports and application grants before
permission enforcement. Old sessions without the protected claim must sign in
again. Activation requires a reviewed deployment and real gateway/Auth/OIDC
acceptance, including new/existing players, interruptions, expiry, revoke,
concurrency, wrong identity/campaign denial and no access expansion. A native
flow render or a policy unit test is not an authenticated browser rehearsal.

The disabled implementation shipped in #6867 as monolith `0.649.9`, with
migration `20261005060000` applied and all seven platform tables empty.
The first activation stage enables private operator management and protected
identity claims. Enrollment and enforcement remain off until the operator has
explicitly imported its signed identity and received the two Grimoire grants.
A fresh Authentik login is required to obtain the new claims. Registered-player
campaign links remain enabled throughout. Subsequent signup activation and
real browser acceptance remain tracked on #6858.
