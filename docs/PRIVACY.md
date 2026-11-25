# Privacy model

The recordings are of children. Treat the whole tool as a privacy product.

1. **Local only.** Whisper, the LLM and the review app run on your Mac. The code makes no network calls except to `localhost`.
2. **Names live in `config.local.json`** (gitignored), never in code. `name_groups` lists every spelling of a person so ASR is biased towards them and checks treat them as one.
3. **Sensitive screening.** A local LLM pass flags cards that mention health, school, location or other private detail so they are not published by default.
4. **Published site, defence in depth** (what we run, adapt to taste): every page `noindex` + `robots.txt` deny-all; gated pages carry no link-preview metadata; no `og:image`; audio is family-tier only (a post-build gate fails the build otherwise); unlisted public pages use unguessable tokens with no index page.
5. **Release gate.** `scripts/pii_gate.py <dir> --deny your-denylist.json` fails on home paths, emails, phone numbers, IPs, key patterns and any word on *your private* denylist. Run it before every release; keep the denylist out of the repo.
6. **No history.** Publish from a fresh tree (`git init`), never from a repo whose history ever held `data/`, exports or names.
