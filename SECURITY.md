# Security

This service accepts account access tokens. Run it only on a trusted host and
do not expose port 15336 directly to the public internet without adding TLS and
authentication in front of it.

- Never commit tokens, credential files, proxy passwords or generated runtime data.
- The Compose default binds to `0.0.0.0`. Set `UPI_BIND_HOST=127.0.0.1`
  when the service should only be reachable through a local reverse proxy or
  an SSH tunnel.
- Set a strong `UPI_ADMIN_PASSWORD` and an independent random
  `UPI_SESSION_SECRET`; never reuse the account access token as either value.
- Set `UPI_COOKIE_SECURE=1` when the site is served through HTTPS.
- Rotate a token immediately if it appears in logs, screenshots or issue reports.
- Report security issues privately to the repository owner rather than opening
  an issue containing credentials.
