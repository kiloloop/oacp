# Project Facts

## What

parcel-api — REST service for parcel tracking: shipments, scan events,
courier webhooks, CSV export. FastAPI + PostgreSQL 16 + Redis (idempotency
keys). Single-region deploy for v1.

## Agent Roles
- Implementer (API + data model): claude
- QA/Reviewer: codex
- Deploy/Ops + memory curation: nova

## Protocol
- Shared handoff protocol version: v0.2.0

## Conventions
- Timestamps: UTC ISO-8601 (RFC 3339) on every client-visible surface — org
  rule, see `../../../org-memory/rules.md`
- Migrations: Alembic, one revision per PR, no autogenerate in CI
- Review loop: every PR gets a codex findings packet before merge
