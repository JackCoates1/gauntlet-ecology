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
    '{"scenario":"protected-note-vault","harness_event_log_version":"1.0","objective":"attacker attempts the protected note boundary while defender preserves confidentiality and availability","defender_contract":"read_note(note_id, token) -> str","attacker_contract":"attack(request)","request_budget":20,"notes":{"storage":"ephemeral fixture vault","network":"isolated sandbox"}}'::jsonb,
    '1.0',
    'Protected Note Vault: attack and defend an isolated note service while preserving access controls and availability.'
) ON CONFLICT (semver) DO NOTHING;
