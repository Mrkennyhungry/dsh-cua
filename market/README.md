# Market index

`index.json` is a DSH plugin market index in the `format: 1` document that
`@deepseek-ai/dsh-plugin-market-index` validates and `@deepseek-ai/dsh-plugin-market`
reads. DSH has no official market: the Discover tab lists the index sources a user
adds, so this file is what makes the plugin discoverable.

Add this URL as an index source (**Plugins → More → Custom plugin markets →
Discover**):

```
https://raw.githubusercontent.com/Mrkennyhungry/dsh-cua/main/market/index.json
```

Rules this file follows, so a future edit stays installable:

- `versions[].source` is `git`, with a canonical `https://github.com/...` URL and a
  lowercase 40-hex commit SHA. Tags are not accepted by the format; the commit is
  the immutable pin.
- `publishTimestamp` is Unix **seconds**, not milliseconds.
- `icon` is relative and resolves against this file's URL, which keeps it on the
  same origin as the index (the format rejects a cross-origin `http:` icon).
- `metadata.categories`, `metadata.labels` and `metadata.sortKeys` are declared at
  the top level; plugins may only reference ids declared there.
- `description`/`title` objects must contain an `en` member; `zh` is optional.

Revalidate after any edit with the shipped validator, not by eye: the failure mode
is a silently rejected index, and the parser reports the JSON path it refused.
