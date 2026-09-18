#!/usr/bin/env node
// Reading the pages the scoring rules cannot settle, with a local model.
//
//   node adjudicate.mjs <sample|eval|rule|status> [flags]
//
// The sweep (timeliness_check.py) writes a page here whenever its wording
// checks find nothing and no date can be read off it either. That is a narrow
// band, fewer than one listing in twenty, and it is the only band where a
// reading changes anything, because everywhere else something deterministic
// has already decided.
//
// Nothing in this file writes to Airtable. It turns queued pages into rulings
// in a ledger; apply_adjudications.py decides what, if anything, goes back.
//
//   adjudication/queue.jsonl     pages awaiting a reading, from the sweep
//   adjudication/verdicts.jsonl  one ruling per record, with its evidence
//   eval/adjudication-set.jsonl  labelled pages, the accuracy measurement
//   eval/adjudication-eval.json  what the last eval measured, per model
//
// `rule` refuses to run until an eval exists for the model it is about to use.
// The order is deliberate and is the whole reason this is a separate pass: a
// number that nobody measured is worse than no number, because it looks the
// same as a measured one in the ledger.
//
// Inference goes through CTFG-curator's local-llm.mjs, unchanged: preflight()
// checks the model is resident with a window big enough for the prompt plus
// the reply, dealer() spreads calls over however many instances are loaded,
// and nothing here loads or unloads anything. The host is shared.
//
// Set CTFG_LLM_BASE_URL and CTFG_LLM_MODEL to point it at a different host or
// model; both are read by local-llm.mjs.

import { mkdirSync, existsSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

// CTFG-curator is a sibling checkout, and it owns the client for the local
// model host. Copying that file in here would give two copies to keep in step,
// and the lessons in its header were expensive enough the first time.
const LOCAL_LLM = process.env.CTFG_LOCAL_LLM
  ?? join(__dirname, "..", "CTFG-curator", "lib", "local-llm.mjs");

let preflight, complete, dealer, roughTokens;
try {
  ({ preflight, complete, dealer, roughTokens } = await import(LOCAL_LLM));
} catch (e) {
  console.error(`Cannot load ${LOCAL_LLM}\n  ${e.message}\n` +
    `  Set CTFG_LOCAL_LLM to CTFG-curator's lib/local-llm.mjs if the checkout is elsewhere.`);
  process.exit(1);
}

const DIR    = join(__dirname, "adjudication");
const EVAL   = join(__dirname, "eval");
const QUEUE  = join(DIR, "queue.jsonl");
const LEDGER = join(DIR, "verdicts.jsonl");
const EVAL_SET     = join(EVAL, "adjudication-set.jsonl");
const EVAL_RESULTS = join(EVAL, "adjudication-eval.json");

// The same default the curator's backfill uses. The page is capped at 6,000
// characters by the sweep, so a prompt is around 2,000 tokens and the reply a
// couple of hundred; 12,000 leaves room for a page that is all long words.
const NEED_CTX = 12_000;
const DEFAULT_MODEL = "openai/gpt-oss-120b";

// Below this many labelled pages the eval is a vibe, not a measurement: at 25
// rows one disagreement moves the accuracy by four points.
const EVAL_MIN_ROWS = 25;

const VERDICTS = ["finished", "running", "unclear"];

const COMMANDS = ["sample", "eval", "rule", "status"];
const argv = process.argv.slice(2);
const CMD  = argv[0];
const has  = (f) => argv.includes(f);
const str  = (f, d = null) => {
  const i = argv.indexOf(f);
  if (i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--")) return argv[i + 1];
  const eq = argv.find(a => a.startsWith(`${f}=`));
  return eq ? eq.slice(f.length + 1) : d;
};
const num = (f, d) => { const v = str(f); return v == null ? d : Number(v); };

if (!COMMANDS.includes(CMD)) {
  console.error(`Usage: node adjudicate.mjs <${COMMANDS.join("|")}> [flags]

  sample  Pull pages out of the queue into eval/adjudication-set.jsonl for
          labelling by hand. Every row lands with label: null; a row with no
          label is ignored by eval, so a half-finished set measures only what
          has actually been read.
          --n N        pages to pull (default 35)
          --refresh    replace the existing set rather than refusing

  eval    Rule every labelled page in the eval set and compare, then write
          eval/adjudication-eval.json. Prints the confusion matrix and, on its
          own line, the number that decides whether this is usable: how often
          the model said "finished" about a project that had not finished.
          --model <id>       a model id on the local host (default ${DEFAULT_MODEL})
          --concurrency N    requests in flight (default: 2 per instance)

  rule    Read every queued page not already ruled → adjudication/verdicts.jsonl
          --model <id>       a model id on the local host (default ${DEFAULT_MODEL})
          --limit N          stop after N pages
          --concurrency N    requests in flight (default: 2 per instance)
          --ids <a,b,c>      rule exactly these record ids
          --redo             re-rule records already in the ledger
          --dry              print the rulings and write no ledger (the one way
                             to run without an eval)

  status  What is queued, what has been ruled, and what the eval measured.`);
  process.exit(1);
}

const jsonl = (file) => existsSync(file)
  ? readFileSync(file, "utf8").split("\n").filter(Boolean).map(l => JSON.parse(l))
  : [];

const writeJsonl = (file, rows) =>
  writeFileSync(file, rows.map(r => JSON.stringify(r)).join("\n") + (rows.length ? "\n" : ""));

/**
 * The newest queue row per record id.
 *
 * The same listing comes round again on every sweep, so a record can be queued
 * more than once. Only the newest row is worth ruling: the older one's page
 * text is a copy of a site as it stood months ago, and a verdict read off that
 * would be presented as a reading of the site today.
 */
function latestPerId(rows) {
  const by = new Map();
  for (const r of rows) {
    const prev = by.get(r.id);
    if (!prev || String(r.queued ?? "") >= String(prev.queued ?? "")) by.set(r.id, r);
  }
  return [...by.values()];
}

// ── The question ─────────────────────────────────────────────────────────────
//
// One question about one page. It can decline, and declining is the expected
// answer: the band this pass reads is defined by the rules having failed on it,
// not by the pages being obviously one thing or the other.
//
// The wording is the same question the hosted model was asked before this pass
// existed, kept word for word so the eval measures the model and not a rewrite.

const SYSTEM = `You read the homepage of a civic technology project and answer one \
question: has the project itself finished?

Finished means the thing the listing is about has ended and will not resume. A page saying \
an organisation has wound down, a consultation has closed, a programme has run its course or \
a service has been retired is finished.

It is NOT finished when only an intake window has closed. Registration closing, applications \
closing, a submission deadline passing, nominations closing or voting for one round ending \
are all routine events on a project that is running, and a recurring event closes \
registration precisely because it is about to take place.

Say "unclear" whenever the page does not settle it. Unclear is the right answer for most \
pages and carries no penalty: something else decides those. Only answer "finished" on \
wording that states it, and quote that wording exactly as it appears.

Reply with one JSON object and nothing else:
{"verdict": "finished" | "running" | "unclear", "evidence": "<the page's own words, or an empty string>", "reason": "<one sentence>"}`;

const prompt = (row) =>
  `Project: ${row.name ?? "(unnamed)"}\nAddress: ${row.url ?? "(none)"}\n\nPage text:\n${row.text ?? ""}`;

/**
 * The reply as a ruling, or null when it is not one.
 *
 * A local model is looser than a schema-constrained hosted one: it fences its
 * JSON, it prefixes a sentence, and a reasoning model puts a <think> block in
 * front of the lot. All three are recoverable and none of them is a wrong
 * answer, so they are recovered rather than counted as failures. A reply that
 * still will not parse is left unruled and the page stays in the queue, which
 * is the same outcome as never having asked.
 */
function parseRuling(raw) {
  const text = String(raw ?? "")
    .replace(/<think>[\s\S]*?<\/think>/gi, "")
    .replace(/<\|channel\|>[\s\S]*?<\|message\|>/gi, "")
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```\s*$/, "");

  let data = null;
  try { data = JSON.parse(text); }
  catch { data = firstBalancedObject(text); }
  if (!data || typeof data !== "object") return null;

  const verdict = String(data.verdict ?? "").toLowerCase().trim();
  if (!VERDICTS.includes(verdict)) return null;
  return {
    verdict,
    evidence: String(data.evidence ?? "").slice(0, 160),
    reason:   String(data.reason ?? "").slice(0, 200),
  };
}

/** The longest balanced {...} in the text that actually parses, or null. */
function firstBalancedObject(text) {
  let best = null;
  for (let start = 0; start < text.length; start++) {
    if (text[start] !== "{") continue;
    let depth = 0, inStr = false, esc = false;
    for (let i = start; i < text.length; i++) {
      const ch = text[i];
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === "{") depth++;
      else if (ch === "}" && --depth === 0) {
        const slice = text.slice(start, i + 1);
        try {
          JSON.parse(slice);
          if (!best || slice.length > best.length) best = slice;
        } catch { /* not this one */ }
        break;
      }
    }
  }
  return best === null ? null : JSON.parse(best);
}

/** Run fn over items with at most `limit` in flight. */
async function mapPool(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const i = next++;
      if (i >= items.length) return;
      out[i] = await fn(items[i], i);
    }
  });
  await Promise.all(workers);
  return out;
}

/**
 * Rule a list of queue rows. Returns { rulings, failures }.
 *
 * One page per request, never batched. The curator batches four listings into
 * a request because its prompts are short and its queue is 11,015 records; here
 * the page text is the whole prompt, the queue is in the hundreds, and a batch
 * that comes back covering three of four pages costs more to detect than the
 * requests it saved.
 */
async function rulePages(rows, { model, concurrency = null, onEach = null }) {
  const pf = await preflight({ model, needCtx: NEED_CTX });
  // Two in flight per loaded instance. preflight() has just told us how many
  // there are, so this is read off the host rather than guessed, and asking it
  // again only to size a pool would be a second call for something already in
  // hand.
  const inFlight = concurrency ?? Math.max(2, pf.instances.length * 2);
  console.log(`${pf.model} on ${pf.instances.length} instance(s), ${pf.context}-token window, ` +
    `${inFlight} request(s) in flight`);

  const oversize = rows.filter(r => roughTokens(prompt(r)) > pf.context - 600);
  if (oversize.length) {
    console.warn(`  ${oversize.length} page(s) are too long for the loaded window and are ` +
      `left in the queue rather than silently truncated`);
  }
  const work = rows.filter(r => !oversize.includes(r));

  const next = dealer(pf.instances);
  const rulings = [];
  const failures = [];
  let done = 0;

  await mapPool(work, inFlight, async (row) => {
    let text;
    try {
      ({ text } = await complete({
        model: next(), system: SYSTEM, user: prompt(row), maxTokens: 400,
      }));
    } catch (e) {
      failures.push({ id: row.id, why: e.message.slice(0, 160) });
      return;
    }
    const ruling = parseRuling(text);
    if (!ruling) {
      failures.push({ id: row.id, why: `unparseable reply: ${String(text).slice(0, 120)}` });
      return;
    }
    rulings.push({ row, ...ruling });
    if (onEach) onEach(row, ruling);
    if (++done % 25 === 0) console.log(`  ${done}/${work.length}`);
  });

  return { rulings, failures, model: pf.model, instances: pf.instances.length };
}

// ── sample ───────────────────────────────────────────────────────────────────

function cmdSample() {
  const queue = latestPerId(jsonl(QUEUE));
  if (!queue.length) {
    return console.error(`${QUEUE} is empty. Run the sweep, or download the ` +
      `adjudication-queue artifact from a Timeliness Check run, before sampling.`);
  }
  if (existsSync(EVAL_SET) && !has("--refresh")) {
    const rows = jsonl(EVAL_SET);
    const labelled = rows.filter(r => r.label).length;
    return console.log(`${EVAL_SET} already holds ${rows.length} page(s), ${labelled} labelled. ` +
      `--refresh to replace it.`);
  }

  // A plain random draw, not a hand-picked one. The point of the set is to say
  // how the model does on what the sweep actually queues, so the sampling has
  // to be blind to how hard a page looks: picking the interesting ones measures
  // the interesting ones.
  const n = num("--n", 35);
  const pool = [...queue];
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  const picked = pool.slice(0, Math.min(n, pool.length));

  mkdirSync(EVAL, { recursive: true });
  writeJsonl(EVAL_SET, picked.map(r => ({
    id: r.id, name: r.name, url: r.url, queued: r.queued, text: r.text,
    label: null,       // one of: finished, running, unclear
    why: null,         // the words on the page that settle it, for whoever reads this later
  })));

  console.log(`${picked.length} page(s) → ${EVAL_SET}, drawn at random from ${queue.length} queued.`);
  console.log(`Every row needs a label of finished, running or unclear before eval will count it.`);
  console.log(`Label them by reading the page text in the row, not by opening the site: the`);
  console.log(`model is being measured on the text it will actually be given.`);
}

// ── eval ─────────────────────────────────────────────────────────────────────

async function cmdEval() {
  const rows = jsonl(EVAL_SET);
  const labelled = rows.filter(r => VERDICTS.includes(r.label));
  if (!rows.length) return console.error(`No eval set. Run: node adjudicate.mjs sample`);
  if (!labelled.length) return console.error(`${EVAL_SET} has ${rows.length} page(s) and no labels yet.`);
  if (labelled.length < EVAL_MIN_ROWS) {
    console.warn(`Only ${labelled.length} of ${rows.length} page(s) are labelled. ` +
      `rule will not accept an eval under ${EVAL_MIN_ROWS}.`);
  }

  const model = str("--model", DEFAULT_MODEL);
  const { rulings, failures, instances } =
    await rulePages(labelled, { model, concurrency: num("--concurrency", null) });

  // The matrix, and then the one cell that matters. The costs here are not
  // symmetrical: an "unclear" the model should have called finished leaves a
  // listing scored exactly as it is scored today, while a "finished" on a live
  // project is a proposal to retire it. Both are errors, one is expensive.
  const matrix = {};
  for (const t of VERDICTS) matrix[t] = Object.fromEntries(VERDICTS.map(p => [p, 0]));
  const wrongFinished = [];
  for (const r of rulings) {
    matrix[r.row.label][r.verdict]++;
    if (r.verdict === "finished" && r.row.label !== "finished") {
      wrongFinished.push({ name: r.row.name, url: r.row.url, truth: r.row.label, said: r.evidence });
    }
  }

  const ruled   = rulings.length;
  const agreed  = rulings.filter(r => r.verdict === r.row.label).length;
  const saidFin = rulings.filter(r => r.verdict === "finished").length;
  const trueFin = rulings.filter(r => r.row.label === "finished").length;
  const caught  = rulings.filter(r => r.verdict === "finished" && r.row.label === "finished").length;

  const pct = (a, b) => b ? `${((a / b) * 100).toFixed(0)}%` : "n/a";

  console.log(`\n${ruled} page(s) ruled, ${failures.length} failed to parse or send.`);
  console.log(`\n${"".padEnd(14)}` + VERDICTS.map(p => `said ${p}`.padStart(15)).join(""));
  for (const t of VERDICTS) {
    console.log(`  is ${t.padEnd(11)}` + VERDICTS.map(p => String(matrix[t][p]).padStart(15)).join(""));
  }
  console.log(`\nAgreed with the label on ${agreed} of ${ruled} (${pct(agreed, ruled)}).`);
  console.log(`Said "finished" ${saidFin} time(s); ${wrongFinished.length} of those had not finished.`);
  console.log(`Of ${trueFin} page(s) that had finished, it caught ${caught} (${pct(caught, trueFin)}).`);
  if (wrongFinished.length) {
    console.log(`\nThe expensive errors. Each one is a live project proposed for retirement:`);
    for (const w of wrongFinished) {
      console.log(`  ${w.name} (${w.truth})\n    ${w.url}\n    quoted: ${w.said || "(nothing)"}`);
    }
  }

  const results = existsSync(EVAL_RESULTS) ? JSON.parse(readFileSync(EVAL_RESULTS, "utf8")) : {};
  results[model] = {
    ran: new Date().toISOString().slice(0, 19) + "Z",
    setSize: rows.length, labelled: labelled.length, ruled, instances,
    agreed, failures: failures.length,
    saidFinished: saidFin, falseFinished: wrongFinished.length,
    trueFinished: trueFin, caughtFinished: caught,
    matrix,
  };
  mkdirSync(EVAL, { recursive: true });
  writeFileSync(EVAL_RESULTS, JSON.stringify(results, null, 2) + "\n");
  console.log(`\n→ ${EVAL_RESULTS}`);
}

// ── rule ─────────────────────────────────────────────────────────────────────

/**
 * The eval this model has on record, or a sentence saying why there isn't one.
 *
 * Deliberately not a pass mark. Every "finished" ruling goes to a person before
 * it touches a record, so a threshold here would be a second gate in front of a
 * gate, and picking its number would be inventing a standard rather than
 * measuring one. What this enforces is that the number exists, was measured on
 * this model, and rests on enough pages to mean something.
 */
function evalFor(model) {
  if (!existsSync(EVAL_RESULTS)) {
    return { ok: false, why: `nothing has been evaluated yet (no ${EVAL_RESULTS})` };
  }
  const r = JSON.parse(readFileSync(EVAL_RESULTS, "utf8"))[model];
  if (!r) return { ok: false, why: `"${model}" has never been evaluated` };
  if (r.labelled < EVAL_MIN_ROWS) {
    return { ok: false, why: `"${model}" was evaluated on ${r.labelled} labelled page(s), ` +
      `under the ${EVAL_MIN_ROWS} this needs` };
  }
  return { ok: true, r };
}

async function cmdRule() {
  const model = str("--model", DEFAULT_MODEL);
  const dry   = has("--dry");

  const ev = evalFor(model);
  if (!ev.ok && !dry) {
    console.error(`Refusing to rule: ${ev.why}.\n` +
      `  A verdict nobody has measured reads the same as a measured one in the ledger.\n` +
      `  node adjudicate.mjs sample     then label eval/adjudication-set.jsonl\n` +
      `  node adjudicate.mjs eval --model ${model}\n` +
      `  Or --dry to see what it would say, writing nothing.`);
    process.exit(1);
  }
  if (ev.ok) {
    console.log(`Eval on record for ${model}: agreed with ${ev.r.agreed}/${ev.r.ruled}, ` +
      `${ev.r.falseFinished} false "finished", measured ${ev.r.ran}`);
  }

  const queue = latestPerId(jsonl(QUEUE));
  if (!queue.length) {
    return console.error(`${QUEUE} is empty. Run the sweep, or download the ` +
      `adjudication-queue artifact from a Timeliness Check run.`);
  }
  const already = new Set(jsonl(LEDGER).map(r => r.id));
  const pickIds = (str("--ids") ?? "").split(",").filter(Boolean);

  let work = queue;
  if (pickIds.length) {
    const want = new Set(pickIds);
    work = work.filter(r => want.has(r.id));
  } else if (!has("--redo")) {
    work = work.filter(r => !already.has(r.id));
  }
  const unruled = work.length;
  work = work.slice(0, num("--limit", work.length));
  if (!work.length) {
    // Two different emptinesses, and saying the wrong one sends someone to
    // look at the ledger when the answer is the flag they just typed.
    return console.log(unruled
      ? `--limit cut all ${unruled} page(s) that were left to rule.`
      : `Nothing to rule: all ${queue.length} queued page(s) are already in the ledger.`);
  }
  console.log(`${work.length} page(s) to read, of ${queue.length} queued (${already.size} already ruled).`);

  const ruledAt = new Date().toISOString().slice(0, 19) + "Z";
  const ledgerRow = (row, r) => ({
    id: row.id, name: row.name, url: row.url,
    queued: row.queued, ruled: ruledAt, model,
    verdict: r.verdict, evidence: r.evidence, reason: r.reason,
    // Carried through from the sweep so apply_adjudications.py can see what
    // this ruling would change without re-running the scorer.
    score: row.score, raw_score: row.raw_score,
    activity_status: row.activity_status, status: row.status,
    closed_score: row.closed_score,
    closed_activity_status: row.closed_activity_status,
    closed_status: row.closed_status,
    reasons: row.reasons,
  });

  if (!dry) mkdirSync(DIR, { recursive: true });

  // One append per ruling rather than one write at the end. A run that dies
  // partway keeps every page it has already paid to read, and the next run
  // skips those ids, so the cost of a crash is the page in flight.
  const { rulings, failures } = await rulePages(work, {
    model, concurrency: num("--concurrency", null),
    onEach: dry
      ? (row, r) => console.log(`  ${r.verdict.padEnd(8)} ${row.name}: ${r.evidence || r.reason}`)
      : (row, r) => appendFileSync(LEDGER, JSON.stringify(ledgerRow(row, r)) + "\n"),
  });

  const tally = Object.fromEntries(VERDICTS.map(v => [v, 0]));
  for (const { verdict } of rulings) tally[verdict]++;

  if (dry) {
    console.log(`\nDry run, nothing written. ` +
      VERDICTS.map(v => `${tally[v]} ${v}`).join(", ") + `, ${failures.length} failed.`);
    return;
  }

  console.log(`\n${rulings.length} ruling(s) → ${LEDGER}`);
  console.log(`  ` + VERDICTS.map(v => `${tally[v]} ${v}`).join(", ") +
    (failures.length ? `, ${failures.length} failed and left in the queue` : ""));
  if (tally.finished) {
    console.log(`\n${tally.finished} page(s) read as finished. Those change a score and are ` +
      `held for review:\n  python apply_adjudications.py`);
  }
  for (const f of failures.slice(0, 5)) console.log(`  failed: ${f.id}: ${f.why}`);
}

// ── status ───────────────────────────────────────────────────────────────────

function cmdStatus() {
  const queue  = latestPerId(jsonl(QUEUE));
  const ledger = jsonl(LEDGER);
  const ruled  = new Set(ledger.map(r => r.id));
  const tally  = Object.fromEntries(VERDICTS.map(v => [v, 0]));
  for (const r of ledger) if (r.verdict in tally) tally[r.verdict]++;

  console.log(`Queue:   ${queue.length} page(s) (${jsonl(QUEUE).length} rows before de-duplication)`);
  console.log(`Ruled:   ${ruled.size}, leaving ${queue.filter(r => !ruled.has(r.id)).length} to read`);
  console.log(`         ` + VERDICTS.map(v => `${tally[v]} ${v}`).join(", "));

  const set = jsonl(EVAL_SET);
  console.log(`Eval set: ${set.length} page(s), ${set.filter(r => VERDICTS.includes(r.label)).length} labelled`);
  if (!existsSync(EVAL_RESULTS)) return console.log(`Eval:     never run`);
  const results = JSON.parse(readFileSync(EVAL_RESULTS, "utf8"));
  for (const [model, r] of Object.entries(results)) {
    console.log(`Eval:     ${model}: agreed ${r.agreed}/${r.ruled}, ` +
      `${r.falseFinished} false "finished", caught ${r.caughtFinished}/${r.trueFinished}, ${r.ran}`);
  }
}

if (CMD === "sample") cmdSample();
else if (CMD === "eval") await cmdEval();
else if (CMD === "rule") await cmdRule();
else if (CMD === "status") cmdStatus();
