CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE agent_role AS ENUM ('builder', 'attacker', 'defender');
CREATE TYPE generation_state AS ENUM ('draft', 'open', 'closed', 'archived');
CREATE TYPE strategy_validation_status AS ENUM ('pending', 'valid', 'invalid');
CREATE TYPE policy_verdict AS ENUM ('pending', 'allowed', 'denied');
CREATE TYPE match_status AS ENUM ('scheduled', 'running', 'completed', 'failed', 'cancelled');
CREATE TYPE execution_stage AS ENUM ('build', 'setup', 'attack', 'evaluate');
CREATE TYPE job_status AS ENUM ('queued', 'leased', 'running', 'succeeded', 'failed', 'cancelled');

CREATE TABLE challenge_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    semver TEXT NOT NULL UNIQUE,
    source_ref TEXT NOT NULL,
    image_digest_ref TEXT,
    seed_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    scenario_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    scoring_policy_version TEXT NOT NULL,
    public_description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (semver ~ '^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$')
);

CREATE TABLE generations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    number INTEGER NOT NULL UNIQUE CHECK (number >= 0),
    challenge_version_id UUID NOT NULL REFERENCES challenge_versions(id) ON DELETE RESTRICT,
    state generation_state NOT NULL DEFAULT 'draft',
    random_seed BIGINT NOT NULL,
    population_settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    model_budget_counters JSONB NOT NULL DEFAULT '{}'::jsonb,
    parent_selection_policy TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (closed_at IS NULL OR opened_at IS NULL OR closed_at >= opened_at)
);

CREATE TABLE agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role agent_role NOT NULL,
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    creation_generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE RESTRICT,
    lineage_root_id UUID REFERENCES agents(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired', 'disqualified')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE RESTRICT,
    parent_strategy_ids UUID[] NOT NULL DEFAULT '{}',
    manifest_version TEXT NOT NULL,
    source_bundle_hash TEXT NOT NULL,
    source_bundle_uri TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    model_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_status strategy_validation_status NOT NULL DEFAULT 'pending',
    policy_verdict policy_verdict NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, generation_id, source_bundle_hash)
);

CREATE TABLE matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attacker_strategy_id UUID NOT NULL REFERENCES strategies(id) ON DELETE RESTRICT,
    defender_strategy_id UUID NOT NULL REFERENCES strategies(id) ON DELETE RESTRICT,
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE RESTRICT,
    challenge_version_id UUID NOT NULL REFERENCES challenge_versions(id) ON DELETE RESTRICT,
    seed BIGINT NOT NULL,
    status match_status NOT NULL DEFAULT 'scheduled',
    sandbox_policy_version TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CHECK (attacker_strategy_id <> defender_strategy_id),
    CHECK (completed_at IS NULL OR completed_at >= scheduled_at),
    UNIQUE (attacker_strategy_id, defender_strategy_id, seed, challenge_version_id)
);

CREATE TABLE executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id UUID NOT NULL REFERENCES matches(id) ON DELETE RESTRICT,
    stage execution_stage NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    runner_image_digest TEXT NOT NULL,
    resource_limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    exit_reason TEXT,
    output_hash TEXT,
    private_log_uri TEXT,
    attestation_signature TEXT,
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
    UNIQUE (match_id, stage, attempt)
);

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id UUID NOT NULL REFERENCES matches(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    virtual_timestamp BIGINT NOT NULL,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    redacted_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash TEXT,
    hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, sequence),
    UNIQUE (hash)
);

CREATE TABLE scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id UUID NOT NULL UNIQUE REFERENCES matches(id) ON DELETE RESTRICT,
    attacker_points NUMERIC(12, 4) NOT NULL DEFAULT 0,
    defender_points NUMERIC(12, 4) NOT NULL DEFAULT 0,
    availability_points NUMERIC(12, 4) NOT NULL DEFAULT 0,
    policy_penalties JSONB NOT NULL DEFAULT '{}'::jsonb,
    exploit_classification TEXT,
    scorer_version TEXT NOT NULL,
    evidence_root_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE selection_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    generation_id UUID NOT NULL REFERENCES generations(id) ON DELETE RESTRICT,
    eligible_match_ids UUID[] NOT NULL DEFAULT '{}',
    aggregate_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    diversity_score NUMERIC(12, 4) NOT NULL,
    ranking_seed BIGINT NOT NULL,
    selected_parent_strategy_ids UUID[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_ref TEXT NOT NULL,
    status job_status NOT NULL DEFAULT 'queued',
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error_class TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL))
);

CREATE INDEX generations_challenge_version_id_idx ON generations (challenge_version_id);
CREATE INDEX agents_creation_generation_id_idx ON agents (creation_generation_id);
CREATE INDEX agents_lineage_root_id_idx ON agents (lineage_root_id);
CREATE INDEX strategies_agent_id_idx ON strategies (agent_id);
CREATE INDEX strategies_generation_id_idx ON strategies (generation_id);
CREATE INDEX matches_generation_id_idx ON matches (generation_id);
CREATE INDEX matches_status_idx ON matches (status);
CREATE INDEX executions_match_id_idx ON executions (match_id);
CREATE INDEX events_match_sequence_idx ON events (match_id, sequence);
CREATE INDEX scores_match_id_idx ON scores (match_id);
CREATE INDEX selection_decisions_generation_id_idx ON selection_decisions (generation_id);
CREATE INDEX jobs_status_lease_until_idx ON jobs (status, lease_until);

CREATE FUNCTION reject_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events are append-only';
END;
$$;

CREATE TRIGGER events_no_update_or_delete
BEFORE UPDATE OR DELETE ON events
FOR EACH ROW EXECUTE FUNCTION reject_event_mutation();

-- PostgreSQL cannot attach a native foreign key to UUID array elements. These
-- constraint triggers give the array reference columns equivalent integrity.
CREATE FUNCTION validate_strategy_parent_ids() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.parent_strategy_ids) AS parent_id
        WHERE NOT EXISTS (SELECT 1 FROM strategies WHERE id = parent_id)
    ) THEN
        RAISE EXCEPTION 'parent_strategy_ids contains an unknown strategy';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER strategies_parent_ids_fk
BEFORE INSERT OR UPDATE OF parent_strategy_ids ON strategies
FOR EACH ROW EXECUTE FUNCTION validate_strategy_parent_ids();

CREATE FUNCTION validate_selection_reference_ids() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM unnest(NEW.eligible_match_ids) AS match_id
        WHERE NOT EXISTS (SELECT 1 FROM matches WHERE id = match_id)
    ) OR EXISTS (
        SELECT 1 FROM unnest(NEW.selected_parent_strategy_ids) AS strategy_id
        WHERE NOT EXISTS (SELECT 1 FROM strategies WHERE id = strategy_id)
    ) THEN
        RAISE EXCEPTION 'selection decision contains an unknown match or strategy';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER selection_decisions_reference_ids_fk
BEFORE INSERT OR UPDATE OF eligible_match_ids, selected_parent_strategy_ids ON selection_decisions
FOR EACH ROW EXECUTE FUNCTION validate_selection_reference_ids();
