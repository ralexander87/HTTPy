# Security Policy

## Project Scope

This project is designed for short-lived, trusted local-network file sharing.
It is not a hardened internet-facing service.

The browser UI supports upload, download, delete, runtime settings changes, and
CLI command execution. Anyone who can reach the server can use those actions.

## Supported Versions

Security fixes are applied to the current `main` branch.
Older commits and forks may not receive updates.

## Safe Usage Expectations

- Run only on trusted networks.
- Prefer binding to localhost (`--host 127.0.0.1`) when remote access is not needed.
- Stop the server when finished.
- Do not expose this service directly to the public internet.

## Reporting a Vulnerability

Please report potential vulnerabilities privately when possible.

If GitHub Security Advisories are available for this repository, use that
channel first. Otherwise, open an issue with minimal exploit detail and request
private follow-up for sensitive reproduction data.
