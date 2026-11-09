# Security

This project serves one trusted workspace. Its bearer token grants access to all
jobs on that gateway. It is not a multi-tenant service or an untrusted-code sandbox.

Use HTTPS for remote access, keep `.env` private, restrict function aliases, protect
and back up the SQLite database, and apply rate limits at the reverse proxy.
Application inputs and results may contain sensitive data. Modal credentials should
be supplied through your deployment's secret store or a local Modal profile.

Do not publish credentials, job payloads or exploit details in public issues.
Report a vulnerability privately through the repository maintainer's GitHub profile:
https://github.com/lblommesteyn

Only the current 0.1.x release line is maintained. This is an early release; review
the documented single-host and uncertain-submission limits before production use.
