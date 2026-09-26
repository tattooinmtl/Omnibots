// Dev-time only: snapshot Omni's built-in DEFAULT_SETTINGS providers + models
// into omnibots/omni/omni_defaults.json. Omni layers these under the saved
// settings.json on every load, so OmniBots must too. Keys are always blank.
//
// Usage: node tools/omni_defaults_snapshot.mjs [omni install root] [--check]
//   --check  exit 1 if the committed snapshot differs from Omni (used by tests)

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const check = args.includes("--check");
const root = path.resolve(args.find((a) => !a.startsWith("--")) || path.join(process.env.USERPROFILE || process.env.HOME, ".omni"));
const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(here, "..", "omnibots", "omni", "omni_defaults.json");

const { DEFAULT_SETTINGS } = await import(pathToFileURL(path.join(root, "src/core/config.mjs")).href);
const version = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8")).version;

const providers = {};
for (const [name, p] of Object.entries(DEFAULT_SETTINGS.providers || {})) {
  const copy = { ...p, apiKey: p.apiKey === "not-needed" ? "not-needed" : "" };
  if (copy.accounts) copy.accounts = Object.fromEntries(Object.keys(copy.accounts).map((k) => [k, ""]));
  providers[name] = copy;
}
const snapshot = {
  omni_version: version,
  defaultProvider: DEFAULT_SETTINGS.defaultProvider ?? null,
  defaultModel: DEFAULT_SETTINGS.defaultModel ?? null,
  providers,
  models: DEFAULT_SETTINGS.models || {},
};
const text = JSON.stringify(snapshot, null, 2) + "\n";

if (check) {
  const current = fs.existsSync(out) ? fs.readFileSync(out, "utf8").replace(/\r\n/g, "\n") : "";
  const same = JSON.stringify(JSON.parse(current || "{}").providers) === JSON.stringify(providers)
    && JSON.stringify(JSON.parse(current || "{}").models) === JSON.stringify(snapshot.models);
  if (!same) {
    console.error(`omni_defaults.json is out of date with Omni ${version}; run: node tools/omni_defaults_snapshot.mjs`);
    process.exit(1);
  }
  console.log(`omni_defaults.json matches Omni ${version}`);
} else {
  fs.writeFileSync(out, text);
  console.log(`wrote ${out} (Omni ${version}: ${Object.keys(providers).length} providers, ${Object.keys(snapshot.models).length} models)`);
}
process.exit(0);
