#!/usr/bin/env node
// npx create-modal-bridge my-app --target my-modal-app/my_function
// Scaffolds a Worker, deploys the gateway into your Modal workspace, and wires the two together.
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SOURCE = process.env.MODAL_BRIDGE_SOURCE ?? "git+https://github.com/lblommesteyn/modal-bridge";
const args = process.argv.slice(2);
if (args.includes("--help") || args.includes("-h")) {
  console.log(`Usage: npx create-modal-bridge [directory] [--target [alias=]app/function ...] [--skip-deploy]

  directory       where to put the Worker (default: modal-bridge-app)
  --target        a deployed Modal function to expose; repeatable. Omit to be asked.
  --skip-deploy   only scaffold the Worker; deploy later with \`modal-bridge deploy\`
  Other options are passed to \`modal-bridge deploy\` (--name, --rotate, --owner-max-daily, ...).

Needs uv (https://docs.astral.sh/uv/) and a Modal account (\`uvx modal setup\`).`);
  process.exit(0);
}
const skipDeploy = args.includes("--skip-deploy");
const VALUE_FLAGS = new Set(["--target", "--name", "--callback-hosts", "--owner-max-active", "--owner-max-daily"]);
let directory, passthrough = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--skip-deploy") continue;
  if (VALUE_FLAGS.has(args[i])) passthrough.push(args[i], args[++i] ?? "");
  else if (args[i].startsWith("-")) passthrough.push(args[i]);
  else if (!directory) directory = args[i];
  else { console.error(`Unexpected argument: ${args[i]}`); process.exit(1); }
}
directory = resolve(directory ?? "modal-bridge-app");
const run = (command, commandArgs) => spawnSync(command, commandArgs, { stdio: "inherit" }).status === 0;
const bridge = (...commandArgs) => run("uvx", ["--from", SOURCE, "modal-bridge", ...commandArgs]);

if (existsSync(join(directory, "wrangler.jsonc"))) {
  console.log(`Keeping the existing Worker in ${directory}`);
} else {
  cpSync(fileURLToPath(new URL("./template", import.meta.url)), directory, { recursive: true });
  renameSync(join(directory, "gitignore"), join(directory, ".gitignore"));
  console.log(`Created a Worker in ${directory}`);
}
if (skipDeploy) {
  console.log(`\nNext:\n  cd ${directory}\n  uvx --from ${SOURCE} modal-bridge deploy --target your-app/your_function\n  uvx --from ${SOURCE} modal-bridge worker-secrets . --local\n  npm install && npx wrangler dev`);
  process.exit(0);
}

if (spawnSync("uv", ["--version"], { stdio: "ignore" }).status !== 0) {
  console.error("uv is required to deploy the gateway. Install it from https://docs.astral.sh/uv/ and run this again.");
  process.exit(1);
}
const envFile = join(directory, ".modal-bridge.env");
if (!bridge("deploy", "--env-file", envFile, ...passthrough)) process.exit(1);

const env = Object.fromEntries(readFileSync(envFile, "utf8").split("\n").filter(Boolean).map(line => {
  const at = line.indexOf("=");
  return [line.slice(0, at), line.slice(at + 1)];
}));
const alias = Object.keys(JSON.parse(env.BRIDGE_TARGETS))[0];
const config = join(directory, "wrangler.jsonc");
writeFileSync(config, readFileSync(config, "utf8")
  .replace(/"BRIDGE_URL": "[^"]*"/, `"BRIDGE_URL": ${JSON.stringify(env.BRIDGE_URL)}`)
  .replace(/"BRIDGE_FUNCTION": "[^"]*"/, `"BRIDGE_FUNCTION": ${JSON.stringify(alias)}`));
if (!bridge("worker-secrets", directory, "--local", "--env-file", envFile)) process.exit(1);

console.log(`
Ready. Try it locally:
  cd ${directory}
  npm install
  npx wrangler dev
  curl -X POST localhost:8787/jobs -H 'Idempotency-Key: first-job' -d '<json input for ${alias}>'

Ship it:
  uvx --from ${SOURCE} modal-bridge worker-secrets .
  npx wrangler deploy`);
