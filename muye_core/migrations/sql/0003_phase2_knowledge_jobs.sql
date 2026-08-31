-- Phase 2 immutable knowledge build, evaluation, bundle and recoverable job facts.
ALTER TABLE jobs
    ADD COLUMN revision_id TEXT REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT,
    ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    ADD COLUMN cancel_requested_at TIMESTAMPTZ,
    ADD COLUMN completed_at TIMESTAMPTZ,
    ADD COLUMN error_code TEXT;
ALTER TABLE jobs
    ADD CONSTRAINT jobs_status_check CHECK (status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED', 'CANCELLED', 'SUCCEEDED', 'FAILED'));

CREATE TABLE job_events (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (job_id, sequence)
);

CREATE TABLE knowledge_builds (
    build_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT,
    collection_name TEXT NOT NULL UNIQUE,
    document_set_checksum CHAR(64) NOT NULL,
    collection_checksum CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('BUILDING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    UNIQUE (revision_id, document_set_checksum)
);

CREATE TABLE evaluation_runs (
    evaluation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL REFERENCES knowledge_builds(build_id) ON DELETE RESTRICT,
    report_key TEXT,
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    passed BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    UNIQUE (revision_id, build_id)
);

CREATE TABLE revision_bundles (
    revision_id TEXT PRIMARY KEY REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT,
    bundle_checksum CHAR(64) NOT NULL UNIQUE,
    storage_key TEXT NOT NULL UNIQUE,
    runtime_contract_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX jobs_lease_idx ON jobs (status, lease_until, created_at);
CREATE INDEX job_events_job_idx ON job_events (job_id, sequence);
