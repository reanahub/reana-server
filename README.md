# REANA-Server

[![image](https://github.com/reanahub/reana-server/workflows/CI/badge.svg)](https://github.com/reanahub/reana-server/actions)
[![image](https://readthedocs.org/projects/reana-server/badge/?version=latest)](https://reana-server.readthedocs.io/en/latest/?badge=latest)
[![image](https://codecov.io/gh/reanahub/reana-server/branch/master/graph/badge.svg)](https://codecov.io/gh/reanahub/reana-server)
[![image](https://img.shields.io/badge/discourse-forum-blue.svg)](https://forum.reana.io)
[![image](https://img.shields.io/github/license/reanahub/reana-server.svg)](https://github.com/reanahub/reana-server/blob/master/LICENSE)
[![image](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

## About

REANA-Server is a component of the [REANA](http://www.reana.io/) reusable and
reproducible research data analysis platform. It implements the API Server that
takes and performs REST API calls issued by REANA clients.

## Features

- offers rich REST API services for REANA clients
- transmits REST API requests to appropriate REANA cloud components
- REST API to run research analysis workflows on compute clouds
- REST API to list submitted workflows and enquire about their statuses
- REST API to manage analysis files
- REST API to download results of finished analysis workflows
- REST API to find the differences between two workflows (`git` like output)

## Usage

The detailed information on how to install and use REANA can be found in
[docs.reana.io](https://docs.reana.io).

## Authentication and revocation lifecycle

### Upgrading existing user identities

Accounts created before OIDC/JWT authentication have no linked identity
(`idp_issuer`/`idp_subject` are unset) and **cannot sign in through the new flow
until linked to an identity from the configured issuer**. On upgrade each such
user's first login fails with a clear "an account already exists"
`ProvisioningError` rather than a confusing crash, but they remain unable to
sign in until an administrator resolves it — this affects every pre-existing
account on any installation with real users, not just an edge case. Fresh
installs are unaffected.

Two ways to resolve it, and they can be combined:

- **Link accounts by hand**, one at a time, as they need access again:

  ```console
  $ flask reana-admin create-admin-user \
        --email jane.doe@example.org \
        --idp-issuer https://issuer.example.org \
        --idp-subject oidc-subject
  ```

  (works for any existing user, not only admins — see
  `flask reana-admin --help`).

- **Enable automatic email-based linking**, so a user whose verified IdP email
  matches their existing REANA account's email is linked on first login with no
  administrator action:

  ```console
  REANA_AUTH_EMAIL_LINKING_ENABLED=true
  REANA_AUTH_EMAIL_LINKING_ISSUER_ALLOWLIST=https://issuer.example.org
  REANA_AUTH_EMAIL_LINKING_DOMAIN_ALLOWLIST=example.org
  ```

  Disabled by default. This trusts the issuer's `email_verified` claim as proof
  of ownership of a pre-existing REANA account's email address, so only enable
  it for issuers you control or otherwise trust to verify email ownership
  correctly. Both allow-lists are optional and independently applied; an empty
  allow-list skips that particular check rather than rejecting every
  issuer/domain.

REANA Server validates API access tokens statelessly. Issuer, audience,
signature, expiry, and the configured REANA role are checked on every protected
request. Consequently, role removal or account disablement takes effect for
direct API access when the currently issued access token expires; deployments
that need a shorter revocation window must configure that lifetime at the
identity provider.

Browser BFF sessions follow the same access-token rule. Refresh tokens are kept
only in Redis, bound to issuer, subject, and web client, and are removed after
`REANA_AUTH_SESSION_TTL` even if the issuer would keep them longer. Refresh is
accepted only when the issuer returns an access token for the same identity; the
refreshed token's current REANA role is checked before serving the request.
Operators should configure the issuer to reject refresh for disabled accounts.

An already running interactive notebook uses an independent per-session secret.
Identity-provider revocation does not automatically terminate that workload.
Immediate revocation therefore requires closing the interactive session through
REANA, or immediately for every session a user has open:

```console
$ flask reana-admin interactive-session-cleanup --email jane.doe@example.org
```

(`--id` also accepted). This closes sessions right away regardless of activity,
unlike `--days`, which only closes sessions inactive for longer than the given
number of days. The configured inactivity cleanup (`--days`) is the automatic
bound; a value of `forever` deliberately provides no automatic revocation bound
and is not suitable where immediate account revocation is required -- use
`--email`/`--id` for that case instead.

Legacy opaque REANA tokens are not accepted as GitLab webhook credentials. On
upgrade from an opaque-token release, users must delete and recreate existing
GitLab webhooks through REANA once. The new hook receives a separately generated
encrypted per-user secret; no login or API credential is reused.

The secret is a delegated, time-limited capability rather than an OIDC token.
Its authorization expires after `REANA_GITLAB_WEBHOOK_SECRET_MAX_LIFETIME`
seconds (30 days by default), at which point webhook deliveries are rejected
until the user signs in to REANA with their current identity-provider
entitlement and explicitly renews the authorization from their profile. Renewal
changes only the expiry timestamp; it deliberately preserves the secret already
installed in all of the user's GitLab projects. A longer configured lifetime
reduces how often users must renew, but also increases the maximum time for
which a user who loses their REANA role can continue launching workflows through
GitLab. Secrets created before expiry enforcement have no expiry and fail closed
until first renewal.

Operators upgrading an active installation should expect **every pre-existing
GitLab webhook to fail closed immediately** on upgrade: the new validator
accepts only the per-user secret with a future expiry, and no existing hook
carries one until its user renews (or recreates it). When deliveries are
rejected for long enough, GitLab may
[auto-disable the hook](https://docs.gitlab.com/user/project/integrations/webhooks/#auto-disabled-webhooks)
— temporarily after a few consecutive failures and permanently after many.
Renewing the REANA authorization restores REANA's acceptance, but a hook GitLab
has permanently disabled additionally needs a successful test delivery from
GitLab before it resumes. REANA cannot detect or repair that disablement itself,
so renewing an already-expired authorization (`PUT /api/gitlab/webhook-token`)
returns a `message` field warning of it, as a pointer back to this manual
recovery step. Plan upgrades of busy installations accordingly, and tell users
to renew promptly so their projects do not cross GitLab's auto-disable
thresholds during an unattended expiry window.

Administrators do not have to wait out an expiry window, but the correct
procedure depends on the incident.

**Offboarding or a suspected account compromise.** Remove the user's
identity-provider entitlement (their `reana:user` role) **first**, then close
the existing webhook authorization:

```console
$ reana-admin gitlab-webhook-revoke --email user@example.org
```

This de-authorizes the secret immediately — clearing its expiry — while keeping
it, so nothing needs to be reconfigured in GitLab. The ordering matters: while
the account still holds the required role it can sign in and renew the preserved
secret, so revoking before the entitlement is removed leaves a window in which
the user restores their own webhook access.

**A leaked webhook secret.** When the secret value itself must be treated as
compromised, additionally delete it:

```console
$ reana-admin gitlab-webhook-revoke --email user@example.org --delete-secret
```

`--delete-secret` permanently invalidates every hook already installed in GitLab
and forces the user to re-enable each project; it is the operation that
invalidates a captured secret value. It does not by itself close the offboarding
window — an account that still holds the role can create a fresh secret the next
time it enables a project — so for offboarding remove the identity-provider
entitlement first as above.

`--dry-run` reports the effect of either form without applying it.

## Useful links

- [REANA project home page](http://www.reana.io/)
- [REANA user documentation](https://docs.reana.io)
- [REANA user support forum](https://forum.reana.io)
- [REANA-Server releases](https://reana-server.readthedocs.io/en/latest#changes)
- [REANA-Server docker images](https://hub.docker.com/r/reanahub/reana-server)
- [REANA-Server developer documentation](https://reana-server.readthedocs.io/)
- [REANA-Server known issues](https://github.com/reanahub/reana-server/issues)
- [REANA-Server source code](https://github.com/reanahub/reana-server)
