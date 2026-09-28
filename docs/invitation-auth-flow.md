# Invitation link authentication flow

New invitation URLs use:

```text
https://HOST/invite-bootstrap.html#token=SECRET
```

The fragment is never included in the browser's HTTP request. This matters on
bigboy, where `deploy/bigboy/docker-compose.bigboy.yml` routes the canonical
host through oauth2-proxy before the SPA. Sending a fragment directly to the
protected `/shared/sessions` route would lose it during the Keycloak redirect.

The bootstrap flow is:

1. oauth2-proxy permits only `/invite-bootstrap.html` and
   `/invite-bootstrap.js` without authentication.
2. The script copies the fragment secret into same-tab, same-origin
   `sessionStorage`, clears the browser URL, and starts SSO with the fixed
   return path `/shared/sessions`. The SSO URL contains no invitation secret.
3. After Keycloak returns, the protected SPA consumes and removes the
   sessionStorage value, then posts it in JSON to
   `/api/simple/recording-sessions/permissions/redeem`.
4. Redemption remains authenticated and binds only the matching invited
   account. Email-bound redemption requires a nonempty, verified matching
   account email, then binds `user_id`; email text alone never authorizes a
   meeting request. The database lookup hashes the presented secret and locks
   the invitation row so the first binding is deterministic.

Outbound invitation email additionally requires an absolute HTTPS public URL.
Only explicit development/test environments may use HTTP, and then only on a
loopback hostname. An invalid or missing public URL fails closed with the safe
`public_url_not_configured` delivery state; the authenticated share UI may
still turn a relative copy-once URL into a same-origin manual copy. SMTP uses
STARTTLS with certificate validation for remote hosts. Plaintext SMTP is
permitted only for loopback in an explicit development/test environment.

The unicorncommander.ai deployment serves the SPA directly and uses the native
OIDC start route; the same bootstrap selects that route by hostname. No proxy
or production configuration was changed live by this lane. Deployment must
ship the frontend static files and bigboy oauth2-proxy skip-route change
together, then smoke-test signed-out and signed-in invitation opens without a
real provider send.

Legacy query-string links remain readable during the bounded v1 transition for
already authenticated users. They cannot gain the new no-log property
retroactively; the approval-gated plaintext scrub does not invalidate them
because v1 UUIDs are resolved through `token_hash`. Operators should rotate
them before the transport cutoff.
