# Security

This service accepts account access tokens. Run it only on a trusted host and
do not expose port 15336 directly to the public internet without adding TLS and
authentication in front of it.

- Never commit tokens, credential files, proxy passwords or generated runtime data.
- The default Docker mapping binds to `127.0.0.1` only.
- Rotate a token immediately if it appears in logs, screenshots or issue reports.
- Report security issues privately to the repository owner rather than opening
  an issue containing credentials.

