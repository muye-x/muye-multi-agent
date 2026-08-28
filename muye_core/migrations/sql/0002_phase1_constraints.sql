-- Harden Phase 1 invariants without modifying an already-applied migration.
ALTER TABLE agent_revisions
    ADD CONSTRAINT agent_revisions_id_checksum_unique UNIQUE (revision_id, checksum),
    ADD CONSTRAINT agent_revisions_status_check CHECK (status IN ('DRAFT', 'REVIEW_REQUIRED', 'APPROVED', 'REJECTED', 'BUILDING', 'EVALUATING', 'READY', 'FAILED'));
ALTER TABLE agent_drafts
    ADD CONSTRAINT agent_drafts_base_revision_fk FOREIGN KEY (base_revision_id) REFERENCES agent_revisions(revision_id) ON DELETE RESTRICT;
ALTER TABLE revision_approvals
    ADD CONSTRAINT revision_approvals_checksum_fk FOREIGN KEY (revision_id, revision_checksum) REFERENCES agent_revisions(revision_id, checksum) ON DELETE RESTRICT;
ALTER TABLE source_assets
    ADD CONSTRAINT source_assets_parse_status_check CHECK (parse_status IN ('PENDING', 'SAFE', 'PARSED', 'FAILED'));
