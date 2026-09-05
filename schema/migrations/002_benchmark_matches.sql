-- A generation's candidate previously played exactly one, self-selected
-- opponent, so a strong result could not be told apart from a weak-opponent
-- fluke. This flag distinguishes that original one-off pairing from the new
-- fixed panel of recent role-appropriate parents each candidate is also
-- benchmarked against, so both can be queried and reported separately.
ALTER TABLE matches ADD COLUMN is_benchmark BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX matches_generation_is_benchmark_idx ON matches (generation_id, is_benchmark);
