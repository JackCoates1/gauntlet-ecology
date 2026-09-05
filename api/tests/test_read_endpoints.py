def test_list_generations(client, seeded_ids):
    response = client.get("/generations")
    assert response.status_code == 200
    assert response.json()[0]["id"] == seeded_ids["generation_id"]
    assert response.json()[0]["match_count"] == 1


def test_get_generation(client, seeded_ids):
    response = client.get(f"/generations/{seeded_ids['generation_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["challenge_semver"] == "1.0.0"
    assert {strategy["display_name"] for strategy in body["strategies"]} == {
        "Red Team One",
        "Blue Team One",
    }
    assert body["strategies"][0]["model_provenance"] == {}


def test_generation_lineage_returns_selection_and_score_trend(client, seeded_ids):
    response = client.get(f"/generations/{seeded_ids['generation_id']}/lineage")
    assert response.status_code == 200
    body = response.json()
    assert body["generation_number"] == 1
    assert body["selection_decision"] is None
    assert body["trend"] == [{
        "generation_id": seeded_ids["generation_id"],
        "number": 1,
        "scored_matches": 1,
        "average_attacker_points": 7.5,
        "average_defender_points": 5.0,
        "attacker_wins": 1,
        "defender_wins": 0,
    }]


def test_list_matches(client, seeded_ids):
    response = client.get("/matches")
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "id": seeded_ids["match_id"],
            "generation_id": seeded_ids["generation_id"],
            "generation_number": 1,
            "status": "completed",
            "is_benchmark": False,
            "seed": 99,
            "scheduled_at": body[0]["scheduled_at"],
            "completed_at": None,
            "attacker_name": "Red Team One",
            "defender_name": "Blue Team One",
            "attacker_points": 7.5,
            "defender_points": 3.0,
            "availability_points": 2.0,
            # attacker_points is a sum of a breach flag (60 or 0) and a
            # separate technique-quality score; no leak was recorded here so
            # the full 7.5 is attributed to quality.
            "attacker_breach_points": 0.0,
            "attacker_quality_points": 7.5,
            "exploit_classification": None,
        }
    ]
    filtered = client.get(f"/matches?generation_id={seeded_ids['generation_id']}")
    assert filtered.status_code == 200
    assert filtered.json() == body


def test_get_match(client, seeded_ids):
    response = client.get(f"/matches/{seeded_ids['match_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["attacker_name"] == "Red Team One"
    assert body["defender_points"] == 3.0
    assert body["executions"][0]["stage"] == "attack"
    assert body["events"] == [{
        "sequence": 0,
        "virtual_timestamp": 0,
        "actor": "attacker",
        "action_type": "request",
        "redacted_payload": {"technique": "token-guess", "response": "denied"},
        "created_at": body["events"][0]["created_at"],
    }]


def test_leaderboard(client):
    response = client.get("/leaderboard")
    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["display_name"] == "Red Team One"
    assert rows[0]["total_points"] == 7.5
    assert rows[0]["role_rank"] == 1
    assert rows[1]["display_name"] == "Blue Team One"
    assert rows[1]["total_points"] == 5.0
    assert rows[1]["role_rank"] == 1


def test_leaderboard_ranks_within_role_not_across_roles(client):
    # A second attacker with fewer points than the existing defender's total
    # must still rank #1 among attackers: attacker and defender points are
    # different scales and must never be compared to each other for ranking.
    import psycopg
    from api.tests.conftest import DATABASE_URL

    generation_id = client.get(f"/generations").json()[0]["id"]
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        agent_id = connection.execute(
            "INSERT INTO agents (role, display_name, creation_generation_id) VALUES ('attacker', 'Weak Attacker', %s) RETURNING id",
            (generation_id,),
        ).fetchone()[0]
        strategy_id = connection.execute(
            """INSERT INTO strategies (agent_id, generation_id, manifest_version, source_bundle_hash, source_bundle_uri, validation_status, policy_verdict)
               VALUES (%s, %s, '1', 'sha256:weak-attacker', 'object://bundles/weak-attacker', 'valid', 'allowed') RETURNING id""",
            (agent_id, generation_id),
        ).fetchone()[0]
        defender_id = connection.execute("SELECT id FROM strategies WHERE source_bundle_uri = 'object://bundles/defender'").fetchone()[0]
        challenge_id = connection.execute("SELECT challenge_version_id FROM generations WHERE id = %s", (generation_id,)).fetchone()[0]
        match_id = connection.execute(
            """INSERT INTO matches (attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version)
               VALUES (%s, %s, %s, %s, 1234, 'completed', '1') RETURNING id""",
            (strategy_id, defender_id, generation_id, challenge_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO scores (match_id, attacker_points, defender_points, availability_points, scorer_version, evidence_root_hash) VALUES (%s, 1, 60, 25, '1.0.0', 'sha256:weak')",
            (match_id,),
        )
    try:
        rows = client.get("/leaderboard").json()
        attacker_rows = [row for row in rows if row["role"] == "attacker"]
        weak = next(row for row in attacker_rows if row["display_name"] == "Weak Attacker")
        strong = next(row for row in attacker_rows if row["display_name"] == "Red Team One")
        assert weak["role_rank"] == 2 and strong["role_rank"] == 1
        # The defender's 85-point total is higher than any attacker's, but
        # that must not affect attacker ranking at all.
        assert weak["total_points"] == 1.0
    finally:
        # This test shares a session-scoped database with every other test in
        # this file; leave it exactly as found.
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute("DELETE FROM scores WHERE match_id = %s", (match_id,))
            connection.execute("DELETE FROM matches WHERE id = %s", (match_id,))
            connection.execute("DELETE FROM strategies WHERE id = %s", (strategy_id,))
            connection.execute("DELETE FROM agents WHERE id = %s", (agent_id,))


def test_dashboard_summarises_current_range_and_role_records(client, seeded_ids):
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["current_generation"]["id"] == seeded_ids["generation_id"]
    assert body["totals"] == {
        "completed_matches": 1,
        "scored_generations": 1,
        "attacker_wins": 1,
        "defender_wins": 0,
    }
    assert body["role_records"] == [
        {"role": "attacker", "matches": 1, "wins": 1, "losses": 0},
        {"role": "defender", "matches": 1, "wins": 0, "losses": 1},
    ]


def test_evolution_returns_score_series_and_selection_data(client, seeded_ids):
    response = client.get("/evolution")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["generation_id"] == seeded_ids["generation_id"]
    assert body[0]["average_attacker_points"] == 7.5
    # No secret leaked in the seeded match, so the whole 7.5 is quality, not breach.
    assert body[0]["average_attacker_breach_points"] == 0.0
    assert body[0]["average_attacker_quality_points"] == 7.5
    assert body[0]["average_confidentiality_points"] == 3.0
    assert body[0]["average_availability_points"] == 2.0
    assert body[0]["selection_metrics"] is None
    # No benchmark matches were seeded for this generation.
    assert body[0]["benchmark_attacker_sample"] == 0
    assert body[0]["benchmark_defender_sample"] == 0
    assert body[0]["benchmark_attacker_avg"] is None
    assert body[0]["benchmark_defender_avg"] is None


def test_strategy_source_reports_unavailable_for_non_file_artifact(client, seeded_ids):
    generation = client.get(f"/generations/{seeded_ids['generation_id']}").json()
    strategy_id = generation["strategies"][0]["id"]
    response = client.get(f"/strategies/{strategy_id}/source")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["source"] is None


def test_read_endpoints_return_not_found_for_missing_records(client):
    assert client.get("/generations/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/matches/00000000-0000-0000-0000-000000000000").status_code == 404


def test_notable_matches_reports_no_data_yet_on_empty_range(client, seeded_ids):
    # The seeded match has no exploit_classification, so there is no breach
    # and no patched-vulnerability pair to find yet.
    response = client.get("/matches/notable")
    assert response.status_code == 200
    body = response.json()
    assert body["breach"] is None
    assert body["patched_vulnerability"] is None


def test_notable_matches_finds_a_real_breach_and_a_real_patch(client, seeded_ids):
    # Build a genuine, minimal parent-was-breached / child-held pair: the same
    # attacker beats a parent defender, then loses to that defender's child.
    import psycopg
    from api.tests.conftest import DATABASE_URL

    generation_id = seeded_ids["generation_id"]
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        challenge_id = connection.execute("SELECT challenge_version_id FROM generations WHERE id = %s", (generation_id,)).fetchone()[0]
        attacker_id = connection.execute("SELECT id FROM strategies WHERE source_bundle_uri = 'object://bundles/attacker'").fetchone()[0]
        parent_defender_id = connection.execute("SELECT id FROM strategies WHERE source_bundle_uri = 'object://bundles/defender'").fetchone()[0]

        child_agent_id = connection.execute(
            "INSERT INTO agents (role, display_name, creation_generation_id) VALUES ('defender', 'Blue Team Two', %s) RETURNING id",
            (generation_id,),
        ).fetchone()[0]
        child_defender_id = connection.execute(
            """INSERT INTO strategies (agent_id, generation_id, parent_strategy_ids, manifest_version, source_bundle_hash, source_bundle_uri, validation_status, policy_verdict)
               VALUES (%s, %s, %s, '1', 'sha256:child-defender', 'object://bundles/child-defender', 'valid', 'allowed') RETURNING id""",
            (child_agent_id, generation_id, [parent_defender_id]),
        ).fetchone()[0]

        breach_match_id = connection.execute(
            """INSERT INTO matches (attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version, scheduled_at, completed_at)
               VALUES (%s, %s, %s, %s, 5001, 'completed', '1', now() - interval '2 hours', now() - interval '1 hour') RETURNING id""",
            (attacker_id, parent_defender_id, generation_id, challenge_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO scores (match_id, attacker_points, defender_points, availability_points, exploit_classification, scorer_version, evidence_root_hash) VALUES (%s, 63, 0, 25, 'secret-leak', '1.0.0', 'sha256:breach')",
            (breach_match_id,),
        )
        patch_match_id = connection.execute(
            """INSERT INTO matches (attacker_strategy_id, defender_strategy_id, generation_id, challenge_version_id, seed, status, sandbox_policy_version, completed_at)
               VALUES (%s, %s, %s, %s, 5002, 'completed', '1', now()) RETURNING id""",
            (attacker_id, child_defender_id, generation_id, challenge_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO scores (match_id, attacker_points, defender_points, availability_points, exploit_classification, scorer_version, evidence_root_hash) VALUES (%s, 3, 60, 25, 'no-secret-leak', '1.0.0', 'sha256:patch')",
            (patch_match_id,),
        )
    try:
        body = client.get("/matches/notable").json()
        assert body["breach"]["id"] == str(breach_match_id)
        assert body["patched_vulnerability"] is not None
        assert body["patched_vulnerability"]["parent_match"]["id"] == str(breach_match_id)
        assert body["patched_vulnerability"]["child_match"]["id"] == str(patch_match_id)
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute("DELETE FROM scores WHERE match_id IN (%s, %s)", (breach_match_id, patch_match_id))
            connection.execute("DELETE FROM matches WHERE id IN (%s, %s)", (breach_match_id, patch_match_id))
            connection.execute("DELETE FROM strategies WHERE id = %s", (child_defender_id,))
            connection.execute("DELETE FROM agents WHERE id = %s", (child_agent_id,))
