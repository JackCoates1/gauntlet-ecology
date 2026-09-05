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
            "seed": 99,
            "scheduled_at": body[0]["scheduled_at"],
            "completed_at": None,
            "attacker_name": "Red Team One",
            "defender_name": "Blue Team One",
            "attacker_points": 7.5,
            "defender_points": 3.0,
            "availability_points": 2.0,
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
    assert rows[1]["display_name"] == "Blue Team One"
    assert rows[1]["total_points"] == 5.0


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
    assert body[0]["average_confidentiality_points"] == 3.0
    assert body[0]["average_availability_points"] == 2.0
    assert body[0]["selection_metrics"] is None


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
