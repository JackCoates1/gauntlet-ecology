def test_list_generations(client, seeded_ids):
    response = client.get("/generations")
    assert response.status_code == 200
    assert response.json()[0]["id"] == seeded_ids["generation_id"]
    assert response.json()[0]["match_count"] == 1


def test_get_generation(client, seeded_ids):
    response = client.get(f"/generations/{seeded_ids['generation_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["challenge_semver"] == "0.1.0"
    assert {strategy["display_name"] for strategy in body["strategies"]} == {
        "Red Team One",
        "Blue Team One",
    }


def test_get_match(client, seeded_ids):
    response = client.get(f"/matches/{seeded_ids['match_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["attacker_name"] == "Red Team One"
    assert body["defender_points"] == 3.0
    assert body["executions"][0]["stage"] == "attack"


def test_leaderboard(client):
    response = client.get("/leaderboard")
    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["display_name"] == "Red Team One"
    assert rows[0]["total_points"] == 7.5
    assert rows[1]["display_name"] == "Blue Team One"
    assert rows[1]["total_points"] == 5.0


def test_read_endpoints_return_not_found_for_missing_records(client):
    assert client.get("/generations/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/matches/00000000-0000-0000-0000-000000000000").status_code == 404
