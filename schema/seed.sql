INSERT INTO challenge_versions (
    semver,
    source_ref,
    image_digest_ref,
    seed_policy,
    scenario_config,
    scoring_policy_version,
    public_description
) VALUES (
    '1.0.0',
    'harness/protected-note-vault@1.0',
    'sha256:pending-harness-image-digest',
    '{"mode":"deterministic-per-match","seed_source":"matches.seed"}'::jsonb,
    -- Baseline execution controls in the exact shape the harness's strict
    -- scenario validator accepts.  The original row mixed descriptive
    -- metadata into this field, which made the stored baseline incompatible
    -- with the validator and silently ran the defaults instead.
    '{"request_budget":20,"decoy_note_count":0,"token_length":16}'::jsonb,
    '1.0',
    'Protected Note Vault: attack and defend an isolated note service while preserving access controls and availability.'
) ON CONFLICT (semver) DO NOTHING;
