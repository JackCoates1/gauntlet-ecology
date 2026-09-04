const app = document.querySelector("#app");

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function link(text, hash, className = "") {
  const element = node("a", className, text);
  element.href = hash;
  return element;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
}

function formatPoints(value) {
  return value === null || value === undefined ? "Pending" : Number(value).toFixed(2);
}

function status(value) {
  return node("span", `status ${String(value || "unknown").toLowerCase()}`, value || "unknown");
}

function empty(message) {
  return node("p", "empty", message);
}

function detail(label, value) {
  const item = node("div", "detail");
  item.append(node("span", "detail-label", label), node("span", "detail-value", value ?? "—"));
  return item;
}

function heading(title, description) {
  const intro = node("section", "page-intro");
  const copy = node("div");
  copy.append(node("p", "eyebrow", "Live Postgres results"), node("h1", "", title));
  if (description) copy.append(node("p", "lede", description));
  intro.append(copy);
  return intro;
}

async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    const message = response.status === 404 ? "This record no longer exists." : `API request failed (${response.status}).`;
    throw new Error(message);
  }
  return response.json();
}

function table(headers, rows) {
  const wrapper = node("div", "panel table-wrap");
  const element = document.createElement("table");
  const header = document.createElement("thead");
  const headerRow = document.createElement("tr");
  headers.forEach((value) => headerRow.append(node("th", "", value)));
  header.append(headerRow);
  const body = document.createElement("tbody");
  rows.forEach((cells) => {
    const row = document.createElement("tr");
    cells.forEach(({ content, className = "" }) => {
      const cell = node("td", className);
      if (content instanceof Node) cell.append(content);
      else cell.textContent = content ?? "—";
      row.append(cell);
    });
    body.append(row);
  });
  element.append(header, body);
  wrapper.append(element);
  return wrapper;
}

function matchesTable(matches) {
  if (!matches.length) return empty("No matches have been scheduled for this view yet.");
  return table(
    ["Match", "Generation", "Attacker", "Defender", "Status", "Score", "Completed"],
    matches.map((match) => [
      { content: link("View match", `#/matches/${match.id}`) },
      { content: `#${match.generation_number}` },
      { content: match.attacker_name },
      { content: match.defender_name },
      { content: status(match.status) },
      { content: match.attacker_points === null ? "Pending" : `${formatPoints(match.attacker_points)} / ${formatPoints(match.defender_points)} + ${formatPoints(match.availability_points)}`, className: "number" },
      { content: formatDate(match.completed_at) },
    ]),
  );
}

function section(title, content, note) {
  const element = node("section", "section");
  const sectionHeading = node("div", "section-heading");
  sectionHeading.append(node("h2", "", title));
  if (note) sectionHeading.append(node("span", "subtle", note));
  element.append(sectionHeading, content);
  return element;
}

async function renderHome() {
  app.replaceChildren(node("p", "loading", "Loading current standings…"));
  try {
    const [leaderboard, generations, matches] = await Promise.all([
      api("/leaderboard"), api("/generations"), api("/matches"),
    ]);
    const refresh = node("button", "refresh", "Refresh results");
    refresh.type = "button";
    refresh.addEventListener("click", renderHome);
    const intro = heading("The range, in motion.", "Current standings, generation populations, and sandboxed match outcomes from the system of record.");
    intro.append(refresh);
    const leaderboardContent = leaderboard.length
      ? table(
        ["Rank", "Competitor", "Role", "Scored matches", "Total points"],
        leaderboard.map((entry, index) => [
          { content: `#${index + 1}`, className: "rank" },
          { content: entry.display_name },
          { content: status(entry.role) },
          { content: entry.scored_matches, className: "number" },
          { content: formatPoints(entry.total_points), className: "number" },
        ]),
      )
      : empty("No scored strategies yet. Standings will appear after the first completed match.");
    const generationContent = generations.length ? node("div", "generation-list") : empty("No generations have been created yet.");
    generations.forEach((generation) => {
      const card = link("", `#/generations/${generation.id}`, "generation-card");
      const meta = node("div", "card-meta");
      meta.append(status(generation.state), node("span", "", `${generation.match_count} match${generation.match_count === 1 ? "" : "es"}`), node("span", "", `Challenge ${generation.challenge_semver}`));
      card.append(node("div", "generation-number", `Generation ${generation.number}`), meta);
      generationContent.append(card);
    });
    app.replaceChildren(intro, section("Leaderboard", leaderboardContent, "Completed scores only"), section("Generations", generationContent), section("Recent matches", matchesTable(matches), "Latest 100"));
  } catch (error) {
    renderError(error);
  }
}

async function renderGeneration(id) {
  app.replaceChildren(node("p", "loading", "Loading generation…"));
  try {
    const [generation, matches] = await Promise.all([api(`/generations/${id}`), api(`/matches?generation_id=${encodeURIComponent(id)}`)]);
    const details = node("div", "details-grid");
    details.append(detail("State", generation.state), detail("Challenge", generation.challenge_semver), detail("Random seed", String(generation.random_seed)), detail("Opened", formatDate(generation.opened_at)), detail("Closed", formatDate(generation.closed_at)));
    const strategies = generation.strategies.length
      ? table(
        ["Competitor", "Role", "Validation", "Policy", "Created"],
        generation.strategies.map((strategy) => [
          { content: strategy.display_name },
          { content: status(strategy.role) },
          { content: status(strategy.validation_status) },
          { content: status(strategy.policy_verdict) },
          { content: formatDate(strategy.created_at) },
        ]),
      )
      : empty("This generation has no strategies yet.");
    const intro = heading(`Generation ${generation.number}`, generation.public_description);
    app.replaceChildren(link("← All live results", "#/", "back"), intro, details, section("Population", strategies), section("Matches", matchesTable(matches)));
  } catch (error) {
    renderError(error);
  }
}

async function renderMatch(id) {
  app.replaceChildren(node("p", "loading", "Loading match…"));
  try {
    const match = await api(`/matches/${id}`);
    const intro = heading(`${match.attacker_name} vs ${match.defender_name}`, `Generation match · ${match.status}`);
    const details = node("div", "details-grid");
    details.append(detail("Generation", String(match.generation_id)), detail("Seed", String(match.seed)), detail("Scheduled", formatDate(match.scheduled_at)), detail("Completed", formatDate(match.completed_at)), detail("Sandbox policy", match.sandbox_policy_version), detail("Scorer", match.scorer_version));
    const scores = node("div", "score-grid");
    [["Attacker points", match.attacker_points, ""], ["Defender points", match.defender_points, ""], ["Availability points", match.availability_points, "availability"]].forEach(([label, points, className]) => {
      const score = node("div", `score-card ${className}`);
      score.append(node("span", "detail-label", label), node("strong", "", formatPoints(points)));
      scores.append(score);
    });
    const classification = node("div", "classification");
    classification.append(node("span", "detail-label", "Exploit classification"), node("strong", "", match.exploit_classification || "No classification recorded"));
    const executions = match.executions.length
      ? table(
        ["Stage", "Attempt", "Started", "Ended", "Exit reason"],
        match.executions.map((execution) => [
          { content: execution.stage }, { content: execution.attempt, className: "number" }, { content: formatDate(execution.started_at) }, { content: formatDate(execution.ended_at) }, { content: execution.exit_reason },
        ]),
      )
      : empty("No execution summaries were recorded for this match.");
    const scoreDetails = node("section", "section");
    scoreDetails.append(node("h2", "", "Score record"), node("h3", "", "Policy penalties"));
    const penalties = document.createElement("pre");
    penalties.textContent = JSON.stringify(match.policy_penalties ?? {}, null, 2);
    scoreDetails.append(penalties);
    app.replaceChildren(link("← All live results", "#/", "back"), intro, details, section("Score breakdown", scores), classification, scoreDetails, section("Sandbox execution", executions));
  } catch (error) {
    renderError(error);
  }
}

function renderError(error) {
  const message = error instanceof Error ? error.message : "Unable to load live results.";
  app.replaceChildren(link("← All live results", "#/", "back"), node("h1", "", "Results unavailable"), node("p", "error", message));
}

function route() {
  const path = location.hash.replace(/^#/, "") || "/";
  const generation = path.match(/^\/generations\/([0-9a-f-]+)$/i);
  const match = path.match(/^\/matches\/([0-9a-f-]+)$/i);
  if (generation) renderGeneration(generation[1]);
  else if (match) renderMatch(match[1]);
  else renderHome();
  app.focus();
}

window.addEventListener("hashchange", route);
route();
