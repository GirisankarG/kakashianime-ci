# kakashianime-ci

The scheduled health check for KakashiAnime. The site's code and catalogue are
private; only the harness lives here.

It exists because a provider can repurpose itself overnight and keep answering
HTTP 200 with a normal-looking shell. Server 1 was dead on 265,000 pages for a
day before anyone noticed, and only watching the `<video>` element catches
that, so this drives a real browser against every provider on a schedule and
fails loudly.

This repository is public on purpose: Actions minutes are unmetered on public
repositories, and nothing here identifies a host. The provider list arrives
from the `PROVIDERS_JSON` secret at run time, so the harness is public and the
list is not.

## Secrets

| name | what it is |
|---|---|
| `PROVIDERS_JSON` | the provider table, as a JSON array of `{name, tmpl, key}` |
| `SENTRY_DSN` | optional, where a failure is reported |

Without `PROVIDERS_JSON` the script falls back to the private repository's own
table, which is what a developer gets locally and what CI never has.
