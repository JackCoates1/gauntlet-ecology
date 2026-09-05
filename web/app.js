const app = document.querySelector("#app");

function node(tag, className = "", text) {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}
function link(text, hash, className = "") { const element = node("a", className, text); element.href = hash; return element; }
function date(value) { return value ? new Date(value).toLocaleString() : "—"; }
function points(value) { return value === null || value === undefined ? "—" : Number(value).toFixed(1); }
function status(value) { return node("span", `status ${String(value || "unknown").toLowerCase()}`, value || "unknown"); }
function empty(message) { return node("p", "empty", message); }
function detail(label, value) { const item = node("div", "detail"); item.append(node("span", "detail-label", label), node("span", "detail-value", value ?? "—")); return item; }
function heading(kicker, title, description) { const intro = node("section", "page-intro"); const copy = node("div"); copy.append(node("p", "eyebrow", kicker), node("h1", "", title)); if (description) copy.append(node("p", "lede", description)); intro.append(copy); return intro; }
async function api(path) { const response = await fetch(path, { cache: "no-store" }); if (!response.ok) throw new Error(response.status === 404 ? "This record no longer exists." : `API request failed (${response.status}).`); return response.json(); }
function section(title, content, note) { const element = node("section", "section"); const h = node("div", "section-heading"); h.append(node("h2", "", title)); if (note) h.append(node("span", "subtle", note)); element.append(h, content); return element; }

function table(headers, rows) {
  const wrapper = node("div", "panel table-wrap"); const element = document.createElement("table");
  const header = document.createElement("thead"); const headerRow = document.createElement("tr"); headers.forEach((value) => headerRow.append(node("th", "", value))); header.append(headerRow);
  const body = document.createElement("tbody"); rows.forEach((cells) => { const row = document.createElement("tr"); cells.forEach(({ content, className = "" }) => { const cell = node("td", className); content instanceof Node ? cell.append(content) : cell.append(document.createTextNode(content ?? "—")); row.append(cell); }); body.append(row); });
  element.append(header, body); wrapper.append(element); return wrapper;
}
function matchesTable(matches) {
  if (!matches.length) return empty("No matches have been scheduled for this view yet.");
  return table(["Match", "Generation", "Attacker", "Defender", "Result", "Completed"], matches.map((match) => [
    { content: link("Replay", `#/matches/${match.id}`, "text-link") }, { content: link(`#${match.generation_number}`, `#/generations/${match.generation_id}`) }, { content: match.attacker_name }, { content: match.defender_name },
    { content: match.attacker_points === null ? status(match.status) : `${points(match.attacker_points)} / ${points(match.defender_points)} + ${points(match.availability_points)}`, className: "number" }, { content: date(match.completed_at) },
  ]));
}
function metric(label, value, note = "") { const card = node("article", "metric-card"); card.append(node("span", "detail-label", label), node("strong", "", value), node("span", "metric-note", note)); return card; }
function outcome(attacker, defender, availability) { const attack = Number(attacker || 0); const defence = Number(defender || 0) + Number(availability || 0); return attack > defence ? "Attacker won" : attack < defence ? "Defender held" : "Draw"; }
function renderCode(source) { const escaped = source.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;"); const highlighted = escaped.replace(/(#[^\n]*)/g, '<span class="token-comment">$1</span>').replace(/(&quot;.*?&quot;|'.*?')/g, '<span class="token-string">$1</span>').replace(/\b(def|return|if|else|elif|for|in|while|try|except|import|from|True|False|None)\b/g, '<span class="token-keyword">$1</span>'); const code = node("code", "language-python"); code.innerHTML = highlighted; const pre = node("pre", "source-code"); pre.append(code); return pre; }

async function renderHome() {
  app.replaceChildren(node("p", "loading", "Loading the live range…"));
  try {
    const [dashboard, leaderboard, generations, matches] = await Promise.all([api("/dashboard"), api("/leaderboard"), api("/generations"), api("/matches")]);
    const refresh = node("button", "refresh", "Refresh"); refresh.type = "button"; refresh.addEventListener("click", renderHome);
    const intro = heading("Live security range", "The ecosystem in motion.", "Every result below comes from a sandboxed match recorded in Postgres."); intro.append(refresh);
    const current = dashboard.current_generation; const role = Object.fromEntries(dashboard.role_records.map((record) => [record.role, record])); const metrics = node("div", "metric-grid");
    metrics.append(metric("Current generation", current ? `#${current.number}` : "—", current ? `${current.state} · ${current.match_count} matches` : "Waiting for first generation"), metric("Completed matches", String(dashboard.totals.completed_matches), `${dashboard.totals.scored_generations} scored generations`), metric("Attacker record", role.attacker ? `${role.attacker.wins}–${role.attacker.losses}` : "—", "wins / losses"), metric("Defender record", role.defender ? `${role.defender.wins}–${role.defender.losses}` : "—", "wins / losses"));
    // Attacker points (max 75) and defender points (max 85) are different
    // scales, so ranking them together in one list would be misleading.
    // Each role is ranked, and shown, only against its own scale.
    const roleStandings = (role, note) => { const rows = leaderboard.filter((entry) => entry.role === role); return rows.length ? table(["Rank", "Competitor", "Matches", "Points"], rows.slice(0, 8).map((entry) => [{ content: `#${entry.role_rank}`, className: "rank" }, { content: entry.display_name }, { content: entry.scored_matches, className: "number" }, { content: points(entry.total_points), className: "number" }])) : empty(`${note} standings will appear after the first completed match.`); };
    const cards = generations.length ? node("div", "generation-list") : empty("No generations have been created yet."); generations.slice(0, 6).forEach((generation) => { const card = link("", `#/generations/${generation.id}`, "generation-card"); const meta = node("div", "card-meta"); meta.append(status(generation.state), node("span", "", `${generation.match_count} matches`), node("span", "", `Challenge ${generation.challenge_semver}`)); card.append(node("div", "generation-number", `Generation ${generation.number}`), meta); cards.append(card); });
    app.replaceChildren(intro, metrics,
      section("Attacker standings", roleStandings("attacker", "Attacker"), "Ranked on attacker points only (max 75: breach + technique quality)"),
      section("Defender standings", roleStandings("defender", "Defender"), "Ranked on defender points only (max 85: confidentiality + availability)"),
      section("Recent matches", matchesTable(matches.slice(0, 8)), "Latest outcomes"),
      section("Generations", cards, "Open one for strategy source and score detail"),
      node("p", "subtle", link("See a real breach and a patched vulnerability →", "#/notable")));
  } catch (error) { renderError(error); }
}

async function strategyPanel(strategy) {
  const panel = node("article", "strategy-panel"); const title = node("div", "strategy-title"); title.append(node("div", "", strategy.display_name), status(strategy.role)); const metadata = node("div", "strategy-meta"); metadata.append(status(strategy.validation_status), status(strategy.policy_verdict), node("span", "", strategy.model_provenance?.model || "model not recorded")); panel.append(title, metadata);
  try { const source = await api(`/strategies/${strategy.id}/source`); panel.append(source.available ? renderCode(source.source) : empty("Source artifact is unavailable for this strategy.")); } catch (_) { panel.append(empty("Source could not be loaded.")); } return panel;
}
async function renderGeneration(id) {
  app.replaceChildren(node("p", "loading", "Loading generation…"));
  try {
    const [generation, matches] = await Promise.all([api(`/generations/${id}`), api(`/matches?generation_id=${encodeURIComponent(id)}`)]); const intro = heading(`Generation ${generation.number}`, "Generation detail", generation.public_description); const details = node("div", "details-grid"); details.append(detail("State", generation.state), detail("Challenge", generation.challenge_semver), detail("Seed", String(generation.random_seed)), detail("Opened", date(generation.opened_at)), detail("Closed", date(generation.closed_at)));
    const strategies = node("div", "strategy-grid"); if (generation.strategies.length) (await Promise.all(generation.strategies.map(strategyPanel))).forEach((panel) => strategies.append(panel));
    const outcomes = matches.length ? table(["Match", "Kind", "Attacker", "Defender", "Outcome", "Confidentiality", "Availability", "Breach", "Attack quality"], matches.map((match) => [{ content: link("Open replay", `#/matches/${match.id}`) }, { content: match.is_benchmark ? "Benchmark" : "Head-to-head" }, { content: match.attacker_name }, { content: match.defender_name }, { content: outcome(match.attacker_points, match.defender_points, match.availability_points) }, { content: points(match.defender_points), className: "number" }, { content: points(match.availability_points), className: "number" }, { content: points(match.attacker_breach_points), className: "number" }, { content: points(match.attacker_quality_points), className: "number" }])) : empty("No matches were recorded for this generation.");
    app.replaceChildren(link("← Dashboard", "#/", "back"), intro, details, section("Strategies", generation.strategies.length ? strategies : empty("No strategies in this generation."), "Static validation and sandbox smoke-test state"), section("Match outcomes", outcomes, "Breach (0 or 60) and attack quality (0-15) are two different things that used to be shown as one combined \"attack quality\" number"));
  } catch (error) { renderError(error); }
}

function eventSummary(event) { const payload = event.redacted_payload || {}; if (event.action_type === "request") return `${payload.technique || "request"}: ${payload.response || payload.reason || "recorded"}`; if (event.action_type === "benign_check") return `${payload.name || "availability check"}: ${payload.passed ? "passed" : "failed"}`; return payload.text || event.action_type; }
async function renderMatch(id) {
  app.replaceChildren(node("p", "loading", "Loading match replay…"));
  try {
    const match = await api(`/matches/${id}`); const intro = heading("Sandbox match replay", `${match.attacker_name} vs ${match.defender_name}`, `${outcome(match.attacker_points, match.defender_points, match.availability_points)} · ${match.is_benchmark ? "Benchmark panel match" : "Head-to-head match"} · Generation ${match.generation_id.slice(0, 8)}`); const scores = node("div", "score-grid"); [["Attacker breach", match.attacker_breach_points, "60 if the secret leaked, else 0"], ["Attacker technique quality", match.attacker_quality_points, "distinct valid techniques, capped at 15"], ["Confidentiality", match.defender_points, "defender points"], ["Availability", match.availability_points, "defender availability"]].forEach(([label, value, note]) => scores.append(metric(label, points(value), note)));
    const timeline = match.events.length ? node("ol", "replay-timeline") : empty("No replay events were stored for this match."); match.events.forEach((event) => { const item = node("li", `replay-event ${event.actor}`); item.append(node("span", "event-sequence", `#${event.sequence}`), node("span", "event-actor", event.actor), node("strong", "", event.action_type.replaceAll("_", " ")), node("span", "event-summary", eventSummary(event))); const payload = node("details", "event-payload"); payload.append(node("summary", "", "Redacted event data"), renderCode(JSON.stringify(event.redacted_payload, null, 2))); item.append(payload); timeline.append(item); });
    const executions = match.executions.length ? table(["Stage", "Attempt", "Started", "Ended", "Exit reason"], match.executions.map((execution) => [{ content: execution.stage }, { content: execution.attempt, className: "number" }, { content: date(execution.started_at) }, { content: date(execution.ended_at) }, { content: execution.exit_reason }])) : empty("No execution summaries were recorded."); const details = node("div", "details-grid"); details.append(detail("Status", match.status), detail("Seed", String(match.seed)), detail("Sandbox policy", match.sandbox_policy_version), detail("Scorer", match.scorer_version), detail("Completed", date(match.completed_at)));
    app.replaceChildren(link("← Dashboard", "#/", "back"), intro, details, section("Score breakdown", scores, "No defender can win by refusing all traffic"), section("Replay", timeline, `${match.events.length} redacted events, in execution order`), section("Sandbox execution", executions), section("Evidence", renderCode(JSON.stringify({ classification: match.exploit_classification, policy_penalties: match.policy_penalties, evidence_root_hash: match.evidence_root_hash }, null, 2))));
  } catch (error) { renderError(error); }
}

function groupByChallenge(rows) {
  // Generations on different challenge versions are not directly comparable
  // (the challenge itself changed), so they must never share one continuous
  // x-axis or be ranked against each other.
  const groups = new Map();
  rows.forEach((row) => { if (!groups.has(row.challenge_semver)) groups.set(row.challenge_semver, []); groups.get(row.challenge_semver).push(row); });
  return groups;
}
function chart(rows) {
  const valid = rows.filter((row) => row.scored_matches > 0 || row.benchmark_attacker_sample > 0 || row.benchmark_defender_sample > 0);
  if (!valid.length) return empty("Scores will chart here after completed matches exist.");
  const width = 760; const height = 280; const pad = 36;
  const fields = ["average_attacker_points", "average_defender_points", "benchmark_attacker_avg", "benchmark_defender_avg"];
  const max = Math.max(1, ...valid.flatMap((row) => fields.map((field) => row[field] || 0)));
  const x = (index) => pad + (valid.length === 1 ? (width - 2 * pad) / 2 : index * (width - 2 * pad) / (valid.length - 1));
  const y = (value) => height - pad - (Number(value || 0) / max) * (height - 2 * pad);
  const line = (field) => { const points = valid.map((row, index) => row[field] === null || row[field] === undefined ? null : `${index ? "L" : "M"}${x(index).toFixed(1)},${y(row[field]).toFixed(1)}`).filter(Boolean); return points.join(" "); };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("class", "evolution-chart");
  [["attacker", "average_attacker_points"], ["defender", "average_defender_points"], ["benchmark-attacker", "benchmark_attacker_avg"], ["benchmark-defender", "benchmark_defender_avg"]].forEach(([name, field]) => { const d = line(field); if (!d) return; const path = document.createElementNS(svg.namespaceURI, "path"); path.setAttribute("class", `chart-line ${name}`); path.setAttribute("d", d); svg.append(path); });
  valid.forEach((row, index) => { const label = document.createElementNS(svg.namespaceURI, "text"); label.setAttribute("x", x(index)); label.setAttribute("y", height - 12); label.setAttribute("text-anchor", "middle"); label.textContent = `G${row.number}`; svg.append(label); });
  return svg;
}
async function renderEvolution() {
  app.replaceChildren(node("p", "loading", "Loading evolutionary history…"));
  try {
    const rows = await api("/evolution");
    const intro = heading("Selection history", "Does the ecosystem improve?", "A role-aware view of the scores that selection decisions use to breed the next generation. The dashed benchmark lines are each candidate's average across a fixed panel of recent opponents — a steadier signal than the single solid head-to-head line, which can move just because that generation's one opponent happened to be weak or strong.");
    const legend = node("div", "chart-legend"); legend.append(
      node("span", "legend attacker", "Attacker head-to-head (breach + quality)"),
      node("span", "legend defender", "Defender head-to-head (confidentiality + availability)"),
      node("span", "legend benchmark-attacker", "Attacker benchmark avg"),
      node("span", "legend benchmark-defender", "Defender benchmark avg"),
    );
    const groups = groupByChallenge(rows);
    const sections = [];
    for (const [semver, groupRows] of groups) {
      const history = table(
        ["Generation", "Matches", "Attacker (breach+quality)", "Defender total", "Benchmark attacker", "Benchmark defender", "Selection"],
        groupRows.map((row) => [
          { content: link(`#${row.number}`, `#/generations/${row.generation_id}`) },
          { content: row.scored_matches, className: "number" },
          { content: points(row.average_attacker_points), className: "number" },
          { content: points(row.average_defender_points), className: "number" },
          { content: row.benchmark_attacker_sample ? `${points(row.benchmark_attacker_avg)} (n=${row.benchmark_attacker_sample})` : "—", className: "number" },
          { content: row.benchmark_defender_sample ? `${points(row.benchmark_defender_avg)} (n=${row.benchmark_defender_sample})` : "—", className: "number" },
          { content: row.selection_metrics?.bootstrap ? "Bootstrap" : row.selected_parent_strategy_ids?.length ? `${row.selected_parent_strategy_ids.length} parents` : "Not recorded" },
        ]),
      );
      const group = node("div", "challenge-version-group");
      group.append(chart(groupRows), legend.cloneNode(true), history);
      sections.push(section(`Challenge ${semver}`, group, `${groupRows.length} generation${groupRows.length === 1 ? "" : "s"}, not comparable to other challenge versions`));
    }
    app.replaceChildren(intro, ...(sections.length ? sections : [empty("No generations have been created yet.")]));
  } catch (error) { renderError(error); }
}
function notableMatchCard(title, match, description) {
  const card = node("article", "panel score-card");
  const heading = node("h3", "", title);
  card.append(heading);
  if (!match) { card.append(empty(description)); return card; }
  card.append(
    node("p", "", `${match.attacker_name} vs ${match.defender_name} — ${outcome(match.attacker_points, match.defender_points, match.availability_points)}`),
    node("p", "subtle", `Generation ${match.generation_number} · ${date(match.completed_at)}`),
    link("Open full replay →", `#/matches/${match.id}`, "text-link"),
  );
  return card;
}
async function renderNotable() {
  app.replaceChildren(node("p", "loading", "Loading notable matches…"));
  try {
    const notable = await api("/matches/notable");
    const intro = heading("Notable matches", "Real breaches and patches, from the actual match history.", "These are not curated highlights or invented examples — each card links to a real recorded match and its full replay.");
    const breachCard = notableMatchCard("Most recent recorded breach", notable.breach, "No completed match has leaked the secret yet. This will populate once one does.");
    let patchSection;
    if (notable.patched_vulnerability) {
      const { parent_match, child_match } = notable.patched_vulnerability;
      const wrap = node("div", "generation-list");
      wrap.append(notableMatchCard("Before: the parent defender was breached", parent_match), notableMatchCard("After: its child held", child_match));
      patchSection = wrap;
    } else {
      patchSection = empty("No recorded case yet where a defender's parent was breached and the child then held against the same attacker. This needs a few more generations of match history to appear — it is not faked in the meantime.");
    }
    app.replaceChildren(link("← Dashboard", "#/", "back"), intro, section("A genuine breach", breachCard), section("A patched vulnerability", patchSection, "Same attacker, parent strategy breached, child strategy held"));
  } catch (error) { renderError(error); }
}
function renderError(error) { app.replaceChildren(link("← Dashboard", "#/", "back"), node("h1", "", "Results unavailable"), node("p", "error", error instanceof Error ? error.message : "Unable to load live results.")); }
function route() { const path = location.hash.replace(/^#/, "") || "/"; const generation = path.match(/^\/generations\/([0-9a-f-]+)$/i); const match = path.match(/^\/matches\/([0-9a-f-]+)$/i); if (generation) renderGeneration(generation[1]); else if (match) renderMatch(match[1]); else if (path === "/evolution") renderEvolution(); else if (path === "/notable") renderNotable(); else renderHome(); app.focus(); }
window.addEventListener("hashchange", route); route();
