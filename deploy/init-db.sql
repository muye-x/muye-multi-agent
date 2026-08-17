-- Muye Multi-Agent Scaffold 数据库初始化脚本
-- 用途：生产环境 PostgreSQL 初始化
-- 执行方式：docker exec -i muye-postgres-1 psql -U muye -d muye < init-db.sql

-- ============================================
-- 1. LangGraph Checkpointer 表（对话历史）
-- ============================================

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);

CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v integer NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint bytea NOT NULL,
    metadata bytea DEFAULT '{}'::bytea NOT NULL
);

-- Checkpointer 主键
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'checkpoints_pkey') THEN
        ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'checkpoint_blobs_pkey') THEN
        ALTER TABLE checkpoint_blobs ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'checkpoint_writes_pkey') THEN
        ALTER TABLE checkpoint_writes ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'checkpoint_migrations_pkey') THEN
        ALTER TABLE checkpoint_migrations ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);
    END IF;
END $$;

-- ============================================
-- 2. Control Server 表（用户、授权、审计）
-- ============================================

CREATE TABLE IF NOT EXISTS control_users (
    user_id text NOT NULL,
    username text NOT NULL,
    password_hash text NOT NULL,
    is_admin boolean DEFAULT false NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (user_id),
    UNIQUE (username)
);

CREATE TABLE IF NOT EXISTS control_sessions (
    access_hash text NOT NULL,
    user_id text NOT NULL,
    refresh_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    PRIMARY KEY (access_hash),
    UNIQUE (refresh_hash),
    FOREIGN KEY (user_id) REFERENCES control_users(user_id)
);

CREATE TABLE IF NOT EXISTS user_agent_grants (
    user_id text NOT NULL,
    agent_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text NOT NULL,
    PRIMARY KEY (user_id, agent_id),
    FOREIGN KEY (user_id) REFERENCES control_users(user_id)
);

CREATE TABLE IF NOT EXISTS control_audit_logs (
    audit_id text NOT NULL,
    actor_id text,
    action text NOT NULL,
    target text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    PRIMARY KEY (audit_id)
);

-- 单管理员约束（只允许一个 admin）
CREATE UNIQUE INDEX IF NOT EXISTS control_single_admin
    ON control_users (is_admin) WHERE is_admin;

-- ============================================
-- 3. 种子数据（可选）
-- ============================================

-- 注意：密码 hash 需要在应用启动时通过 bootstrap_admin 接口创建
-- 这里只创建 hermes 测试用户（无密码，仅供 API 调用）

INSERT INTO control_users (user_id, username, password_hash, is_admin)
VALUES ('usr_hermes_001', 'hermes', '!', FALSE)
ON CONFLICT (user_id) DO NOTHING;

-- 为 hermes 用户授权酒店员工手册子 Agent
INSERT INTO user_agent_grants (user_id, agent_id, created_by)
VALUES ('usr_hermes_001', 'agent_hotel_employee', 'usr_hermes_001')
ON CONFLICT (user_id, agent_id) DO NOTHING;

-- 也为 hermes_test 用户授权（兼容旧配置）
INSERT INTO control_users (user_id, username, password_hash, is_admin)
VALUES ('hermes_test', 'hermes_test', '!', FALSE)
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO user_agent_grants (user_id, agent_id, created_by)
VALUES ('hermes_test', 'agent_hotel_employee', 'hermes_test')
ON CONFLICT (user_id, agent_id) DO NOTHING;

-- 完成
SELECT 'Database initialization completed' AS status;
