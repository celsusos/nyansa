# Production Readiness

This project now runs the deployed control plane on Postgres-backed state, but a
real enterprise deployment still needs infrastructure controls around it.

## Required Before Company-Wide Use

- Run the control plane behind TLS.
- Set `NYANSA_PRODUCTION=true`.
- Set a high-entropy `NYANSA_BOOTSTRAP_SECRET`.
- Use Postgres-backed state via `DATABASE_URL` or `NYANSA_DATABASE_URL`.
- Set a high-entropy `NYANSA_TOKEN_PEPPER`.
- Set either `NYANSA_ATTESTATION_KEY` or `NYANSA_ATTESTATION_KEY_SEED`.
- Bootstrap exactly once, then store root credentials in a secrets manager.
- Rotate the root token after bootstrap.
- Enable `[control_plane]` in agent project config.
- Keep model `base_url` pointed at an internal/self-hosted endpoint.
- Configure usage limits before enabling shared cloud fallback models.
- Keep shared provider keys behind the control plane; do not give unrestricted
  shared keys to interns or general developers.
- Monitor active sessions from the control-plane UI and revoke abandoned or
  suspicious sessions.
- Run shell/file tools inside isolated containers or VMs.
- Restrict filesystem mounts per project/repository.
- Send logs and container metrics to central monitoring.
- Back up Postgres.
- Verify `/ready` before routing traffic.
- Periodically call `/audit/verify`.

## Not Yet Implemented In-Repo

- OIDC/SAML SSO.
- Distributed rate limiting.
- Distributed session heartbeats for multi-instance control planes.
- mTLS between CLI, control plane, and model gateway.
- Immutable external audit storage.
- Containerized tool execution runtime.
- Budget alerting when organizations reach 80% and 95% of model quota.

Those are infrastructure/product layers, not local code defaults. Treat them as
mandatory for GRIDCo/ECG-scale production.
