-- Phase 2 asynchronous Profile and evaluation proposals.
CREATE TABLE profile_proposals (
    proposal_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE RESTRICT,
    draft_version INTEGER NOT NULL CHECK (draft_version > 0),
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id) ON DELETE RESTRICT,
    proposal_json JSONB,
    proposal_checksum CHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CHECK ((proposal_json IS NULL) = (proposal_checksum IS NULL))
);
CREATE INDEX profile_proposals_agent_idx ON profile_proposals (agent_id, created_at DESC);
