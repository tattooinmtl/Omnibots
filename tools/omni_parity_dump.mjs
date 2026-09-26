// Dev-time only: dump what Omni ITSELF loads, in the same shape as
// omnibots/omni/parity.py, so tests can prove the Python bridge matches.
// Keys are never printed, only a sha256 fingerprint (first 16 hex chars).
//
// Usage: node tools/omni_parity_dump.mjs <omni install root>
// Read-only: Omni's loadSettings() only reads when settings.json exists.

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = path.resolve(process.argv[2] || path.join(process.env.USERPROFILE || process.env.HOME, ".omni"));
const home = process.env.OMNI_HOME || path.join(root, "agent");
if (!fs.existsSync(path.join(home, "settings.json"))) {
  console.error(`no settings.json in ${home}; refusing to run (Omni would create one)`);
  process.exit(2);
}
const imp = (p) => import(pathToFileURL(path.join(root, p)).href);

// Keep Omni quiet and side-effect free while we load.
process.chdir(root);
const { loadSettings } = await imp("src/core/config.mjs");
const { loadProjectConfig, loadSkills, loadMcpConfig } = await imp("src/integrations/extras.mjs");
const { loadScannedSkills } = await imp("src/core/skill-index.mjs");

const fp = (k) => (k ? crypto.createHash("sha256").update(String(k), "utf8").digest("hex").slice(0, 16) : "");

const settings = await loadSettings();
const providers = {};
for (const [name, p] of Object.entries(settings.providers || {})) {
  providers[name] = {
    base_url: String(p.baseUrl || ""),
    key_hash: p.apiKey === "not-needed" ? "keyless" : fp(String(p.apiKey || "").trim()),
    native_tools: p.nativeTools !== false,
    reasoning_param: p.reasoningParam ?? null,
    active_account: p.accounts ? (p.activeAccount ?? null) : null,
  };
}
const models = {};
for (const [key, m] of Object.entries(settings.models || {})) {
  if (!m || typeof m !== "object") continue;
  models[key] = {
    provider: m.provider ?? null, id: m.id ?? null, maxTokens: m.maxTokens ?? null,
    vision: m.vision === true, free: m.free === true,
  };
}
const project = loadProjectConfig();
const bundled = loadSkills(project);
const external = loadScannedSkills(project, bundled);
const skills = {};
for (const s of bundled) skills[s.command] = { source: "bundled", description: s.description };
for (const s of external) skills[s.command] = { source: "external", description: s.description };
const mcp = loadMcpConfig(project);
const mcpServers = {};
for (const [name, cfg] of Object.entries(mcp.servers)) {
  mcpServers[name] = { command: cfg.command ?? null, url: cfg.url ?? null };
}
process.stdout.write(JSON.stringify({ providers, models, skills, mcp_servers: mcpServers, default_model: settings.defaultModel ?? null }));
// Omni may have started timers or child processes on import; don't wait on them.
process.exit(0);
