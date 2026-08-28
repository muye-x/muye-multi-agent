-- v3 Phase 1 platform facts.  This database is intentionally independent from v2 Control.
CREATE TABLE core_users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX core_single_admin ON core_users ((is_admin)) WHERE is_admin;

CREATE TABLE core_sessions (
    access_hash TEXT PRIMARY KEY,
    refresh_hash TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES core_users(user_id),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES core_users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMPTZ,
    archived_by TEXT REFERENCES core_users(user_id),
    suspended_at TIMESTAMPTZ,
    suspended_by TEXT REFERENCES core_users(user_id)
);

CREATE TABLE agent_drafts (
    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE RESTRICT,
    base_revision_id TEXT,
    config_json JSONB NOT NULL,
    updated_by TEXT NOT NULL REFERENCES core_users(user_id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE source_assets (
    asset_id TEXT PRIMARY KEY,
    sha256 CHAR(64) NOT NULL UNIQUE,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    media_type TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    parse_status TEXT NOT NULL DEFAULT 'PENDING',
    created_by TEXT NOT NULL REFERENCES core_users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE draft_sources (
    agent_id TEXT NOT NULL REFERENCES agent_drafts(agent_id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL REFERENCES source_assets(asset_id) ON DELETE RESTRICT,
    display_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    PRIMARY KEY (agent_id, asset_id),
    UNIQUE (agent_id, sort_order)
);

CREATE TABLE agent_revisions (
    revision_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE RESTRICT,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    checksum CHAR(64) NOT NULL,
    spec_json JSONB NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES core_users(user_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (agent_id, revision_number),
    UNIQUE (agent_id, checksum)
);
CREATE TABLE revision_sources (
    revision_id TEXT NOT NULL REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT,
    asset_id TEXT NOT NULL REFERENCES source_assets(asset_id) ON DELETE RESTRICT,
    asset_sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (revision_id, asset_id)
);
CREATE TABLE revision_approvals (
    revision_id TEXT PRIMARY KEY REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT,
    revision_checksum CHAR(64) NOT NULL,
    approved_by TEXT NOT NULL REFERENCES core_users(user_id),
    approved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_agent_grants (
    user_id TEXT NOT NULL REFERENCES core_users(user_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT NOT NULL REFERENCES core_users(user_id),
    PRIMARY KEY (user_id, agent_id)
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_type, subject_id, idempotency_key)
);
CREATE TABLE audit_events (
    audit_id TEXT PRIMARY KEY,
    actor_id TEXT REFERENCES core_users(user_id),
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_checksum CHAR(64) NOT NULL,
    status_code INTEGER NOT NULL,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE INDEX agents_created_at_idx ON agents (created_at DESC, agent_id);
CREATE INDEX audit_events_target_idx ON audit_events (target_type, target_id, created_at DESC);
