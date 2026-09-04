# Security Incidents

## SI-001 — Legacy reference credential rendered during environment-key audit

- Timestamp: 2026-09-04T03:10Z
- Scope: sibling `Expert PreFetch/.env`; not tracked by this repository
- Symptom: a lowercase `git_access` assignment containing spacing around `=` did not match the initial value-redaction expression and was rendered in tool output.
- Containment: the line was removed from both ignored Local Assistant Engine `.env` copies; it is not used by any script or agent.
- Required external action: rotate/revoke the affected GitHub token in the Expert PreFetch environment.
- Prevention: environment audits must parse variable names and emit an allowlisted presence result, never transform arbitrary lines for display.
- Repository impact: no credential was committed; `.env` and credential-bearing files remain ignored and excluded from archives.
