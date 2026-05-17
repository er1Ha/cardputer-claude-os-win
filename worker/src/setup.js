// Per-device Agent + Environment provisioning.
//
// We create one Agent and one Environment per device-secret and cache
// their IDs in KV. Subsequent sessions reuse them. Updating the agent
// config later (system prompt, tools, MCP servers) is a separate
// versioned operation handled out-of-band via the Anthropic API; this
// file only handles first-time provisioning.

import { createAgent, createEnvironment, getAgent } from "./anthropic.js";

const AGENT_KEY = (h) => `prov:${h}:agent_id`;
const ENV_KEY = (h) => `prov:${h}:env_id`;

// Keep this conservative: pre-built agent toolset only. MCP servers
// (GitHub, etc.) can be added by editing the agent post-provisioning.
function defaultAgentBody() {
  return {
    name: "Cardputer Pager Agent",
    model: "claude-opus-4-7",
    system: [
      "You are an autonomous coding and research agent invoked from a hand-held",
      "Cardputer pager. You may run for minutes to hours.",
      "",
      "Operating principles:",
      "- Plan briefly, then execute. Prefer doing over discussing.",
      "- Use the bash and file tools to actually change state in your container.",
      "- When the task involves a repo, leave a clean working tree: commit your work",
      "  with a short imperative subject line, no co-author tags.",
      "- Save user-facing artifacts (reports, scripts, notes) into /workspace/out/",
      "  with descriptive filenames. The user's machine syncs this directory.",
      "- Be terse in your assistant turns. The pager has a 240x135 LCD; the user",
      "  reads only the last sentence or two of each turn live. Save details for",
      "  files in /workspace/out/.",
      "- When you finish a task, end your final assistant turn with a single",
      "  one-line summary that fits in ~60 chars. That line is what the pager",
      "  surfaces as the result.",
    ].join("\n"),
    tools: [{ type: "agent_toolset_20260401" }],
    metadata: { source: "cardputer-pager" },
  };
}

function defaultEnvBody() {
  return {
    name: "cardputer-pager-env",
    config: {
      type: "cloud",
      networking: { type: "unrestricted" },
    },
  };
}

export async function ensureAgentAndEnv(env, deviceHash) {
  const [cachedAgent, cachedEnv] = await Promise.all([
    env.INDEX.get(AGENT_KEY(deviceHash)),
    env.INDEX.get(ENV_KEY(deviceHash)),
  ]);
  if (cachedAgent && cachedEnv) {
    return { agentId: cachedAgent, environmentId: cachedEnv, fromCache: true };
  }

  // Provision in parallel — they're independent.
  const [agent, environment] = await Promise.all([
    cachedAgent
      ? getAgent(env.ANTHROPIC_API_KEY, cachedAgent)
      : createAgent(env.ANTHROPIC_API_KEY, defaultAgentBody()),
    cachedEnv
      ? Promise.resolve({ id: cachedEnv })
      : createEnvironment(env.ANTHROPIC_API_KEY, defaultEnvBody()),
  ]);

  await Promise.all([
    env.INDEX.put(AGENT_KEY(deviceHash), agent.id),
    env.INDEX.put(ENV_KEY(deviceHash), environment.id),
  ]);

  return { agentId: agent.id, environmentId: environment.id, fromCache: false };
}
