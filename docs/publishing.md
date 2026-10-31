# Publishing this plugin

Three channels, three different amounts of ceremony. Only the last two need a token.

| Channel | What it needs | State |
|---|---|---|
| Git install (`git+https://…#<commit>`) | an SSH key that can push | works today |
| Plugin market index (`market/index.json`) | nothing — it is a file in this repo | live |
| npm (`dsh-doraemon@<version>`) | a free npm account + an npm token | optional, not published |
| GitHub Release page | a GitHub token, or the web form | live for v0.4.6 |

**npm is optional.** The git spec and the market index are the supported channels and
both work today; npm only shortens the install string to `dsh-doraemon@0.4.6` and makes
the plugin installable from indexes that list npm packages only. Nothing about the
plugin's behaviour depends on it.

Nothing here requires a paid account. The npm registry is free for public packages,
and this repository already exists on GitHub, so the GitHub side is a token, not a
signup.

## One-time: get the two tokens

**npm.** Sign up at <https://www.npmjs.com/signup> (username, email, password, verify
the email — about a minute), then create a token at
<https://www.npmjs.com/settings/~your-username~/tokens> → **Generate New Token** →
**Classic Token** → type **Automation**. It looks like `npm_XXXXXXXX…` and is shown
once.

**GitHub.** You already have the account. Create a token at
<https://github.com/settings/tokens/new?scopes=repo&description=dsh-cua-release> →
scope **repo** (classic) → **Generate token**. It looks like `ghp_XXXX…` and is shown
once.

Both tokens grant write access to your accounts. Do not paste them into files, issues,
or chat logs that persist; if one leaks, delete it on the same page and make a new one.

The GitHub Release needs no token at all — the web form works:
<https://github.com/Mrkennyhungry/dsh-cua/releases/new?tag=v0.4.6>, paste
`docs/release-v0.4.6.md` as the body. `scripts/publish.ps1` is only a convenience.

If the npm **signup** page answers with a "verify you are human" wall, that is the
signup endpoint refusing a data-centre IP, not a problem with this repository: register
from a phone hotspot or another network, then create the token from any machine. The
publish itself runs fine from here.

## Publish

From the repository root:

```powershell
pwsh -File scripts\publish.ps1
```

Or double-click `publish.bat` in the repository root — same thing, with a pause at the
end so the window stays open.

The script asks for each token at a hidden prompt (`NPM_TOKEN` / `GITHUB_TOKEN` work
too, if you prefer environment variables), then:

1. writes a temporary `.npmrc` **in the repo** — already git-ignored — runs
   `pnpm publish --access public`, deletes the file in `finally`, and verifies the
   version resolves from `registry.npmjs.org`;
2. creates the GitHub Release for the tag matching `package.json`'s version, using
   [`docs/release-v<version>.md`](release-v0.4.6.md) as the body;
3. prints what succeeded and what to do next.

Pass `-SkipNpm` or `-SkipRelease` to run only one half. To rewrite the body of a release
that already exists — for instance after editing `docs/release-v0.4.6.md` — add
`-SyncExisting`; without it an existing release is left untouched and the script only
reports that the two differ.

The repo is a git checkout with a `v0.4.6` tag; `pnpm publish` reads the working tree,
so commit first — the script refuses to run on a dirty tree for that reason.

## After npm publishes: add the npm source to the index

The index currently pins the plugin by commit. An npm version is an extra entry, not a
replacement — readers pick the highest compatible version, and each version carries its
own install target:

```json
{
  "version": "0.4.6",
  "publishTimestamp": 1793162262,
  "source": { "type": "npm" }
}
```

`publishTimestamp` is Unix **seconds**. `source.type: "npm"` optionally takes a
`registry` URL. To offer both channels for the same version, use distinct versions
(e.g. a `0.4.6-npm.1` prerelease) — a version string may appear only once per plugin.

## Adding a plugin to the author index

`market/index.json` is titled "Mrkennyhungry's DSH plugins", so it is meant to grow.
Append an object to `plugins[]` and reuse the category/label ids declared in the
top-level `metadata`, or declare new ones there first — a plugin may only reference
declared ids.

## Revalidating after any index edit

Do not publish an index by eye. Validate it with the same parser DSH runs:

```js
// node, with @deepseek-ai/dsh-plugin-market-index installed
import { parseMarketIndex } from '@deepseek-ai/dsh-plugin-market-index'
parseMarketIndex(JSON.parse(body), indexUrl) // throws MarketIndexError with the JSON path
```

A rejected index fails silently in the UI (the Discover tab just shows the fetch
error), which is why the pin is a 40-hex commit and the timestamp is seconds.
