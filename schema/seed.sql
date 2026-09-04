INSERT INTO challenge_versions (
    semver,
    source_ref,
    image_digest_ref,
    seed_policy,
    scenario_config,
    scoring_policy_version,
    public_description
) VALUES (
    '0.1.0',
    'harness/protected-note-vault@0.1.0',
    'sha256:pending-harness-image-digest',
    '{"mode":"deterministic-per-match","seed_source":"match.seed"}'::jsonb,
    '{"scenario":"protected-note-vault","objective":"attacker attempts authorized-note boundary bypass while defender preserves availability and isolation","notes":{"storage":"ephemeral fixture vault","network":"isolated sandbox"}}'::jsonb,
    '0.1.0',
    'Protected Note Vault: attack and defend an isolated note service while preserving access controls and availability.'
) ON CONFLICT (semver) DO NOTHING;
