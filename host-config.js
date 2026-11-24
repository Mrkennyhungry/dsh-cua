import Schema from '@deepseek-ai/schemastery'

export const Config = Schema.object({
  port: Schema.number().default(8766),
  tokenEnv: Schema.string().default('DSH_DESKTOP_BRIDGE_TOKEN'),
  waitSeconds: Schema.number().default(600),
  // Start the local kernel on demand, hidden, instead of asking the user to pre-launch it.
  autoStart: Schema.boolean().default(true),
  // False keeps the bridge a plain HTTP client (tests, or a kernel managed elsewhere).
  manageKernel: Schema.boolean().default(true),
  bootSeconds: Schema.number().default(120),
  // Keep an auto-started kernel alive after DSH exits, so the next session reconnects instantly.
  keepAlive: Schema.boolean().default(true),
  repoRoot: Schema.string().default(''),
  pythonPath: Schema.string().default(''),
  homeDir: Schema.string().default(''),
  // Where kernel state, logs and the optional key file live. Defaults to
  // ~/.dsh/desktop so the plugin never needs write access to its source directory.
  stateDir: Schema.string().default(''),
  // Pre-P1 name for stateDir, still honoured for existing profiles.
  logDir: Schema.string().default(''),
  apiKeyFile: Schema.string().default(''),
  enableExperimentalPageCache: Schema.boolean().default(false),
  // The docked commander is a desktop overlay that stays on top of every window and
  // reports task state. It is off by default: a fresh install should not put a
  // decorative character over the user's screen before they ask for one, and its
  // click picker only works after `scripts/build-companion-ui.ps1` has been run.
  // Set commandFeedback: true to opt in.
  commandFeedback: Schema.boolean().default(false),
  // Runtime-only selection mirror for the Client chip; not a user preference.
  companionSelection: Schema.string().default('').volatile(),
})
