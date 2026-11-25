# Decisions: what was built in November 2026, and why

This file is the record of what was built in November 2026 and why: one entry per decision, with the alternatives that were weighed and the evidence. Read it before you reverse a decision. The figures come from the production database on the dates given. For how things work today, see [onboarding/](onboarding/README.md). For where each table and DAG lives, see [codebase-map.md](onboarding/codebase-map.md) and [pipelines.md](onboarding/pipelines.md).

Two Claude Code sessions worked in parallel for the owner. One built Genizah, account sync and Grafana. The other built the book pipelines, the catalog and the review tools. Their decisions are recorded together here.

---

## 1. Ground rules

### 1.1 Every job runs on the servers, through Airflow
- **Decision.** Pipelines, experiments and checks run on the data box or the apps box. Pipelines run as Airflow DAGs; notebooks run in JupyterHub. Nothing runs from a laptop over an SSH tunnel.
- **Why.** Work done on the servers is logged, retried and visible to the team, and it shows up as lineage in the catalog (see 2.2). The owner set this rule after one-off local runs had produced results nobody else could see.
- **Consequence.** A new process is a DAG, a migration and a view, never a script someone runs by hand.

### 1.2 Unused upstream files moved to `archive/`, not deleted
- **Decision.** 212 files the running stack never imports were moved to `archive/`, with their git history kept: notebooks, research, mockups, NocoDB dev files, the Elastic Beanstalk deploy, the presentation decks and 5 legacy modules. The deploy skips `archive/`.
- **Why.** The repo has to be readable for new people. Deleting the files would have thrown away upstream work that may still be useful. [archive/README.md](../archive/README.md) says what each item was.

### 1.3 `CLAUDE.md` for AI agents
- **Decision.** A short `CLAUDE.md` at the repo root holds the hard rules: no restart of postgres or airflow-* without the owner's OK, migration rules, secrets, commit style. It points to the onboarding docs for everything else.
- **Why.** Agents worked in this repo every day and needed the server rules in front of them. Writing the rules once keeps the two copies from drifting apart.

---

## 2. Platform

### 2.1 Postgres as the system of record, Airflow for every job
This was inherited from the October rebuild ([README](../README.md)), and kept.
- **Since then.** All background work moved into Airflow. In Birddog it had run as threads inside the web app, where it was invisible and was lost whenever the app restarted.
- **Evidence.** Over 17–24 November there were 152 DAG runs, of which 146 succeeded.

### 2.2 Data catalog: Marmot, with lineage from Airflow
- **Decision.** [Marmot](../deploy/marmot/README.md) runs on the apps box: one container, 512 MB, its own database in `apps-postgres`. A daily pipeline catalogs `raw`, `core` and `views`. Airflow sends OpenLineage events to it.
- **Alternatives.**
  - OpenMetadata and DataHub need 5 GB of memory or more.
  - Marquez only shows lineage, with no catalog.
  - Open Data Discovery doesn't support Airflow 3.
  - Amundsen was reported archived.
- **Why.** The owner asked for "a lightweight catalog". OpenLineage is a standard, so Marmot can later be swapped for another catalog without changing any DAG.
- **Details.**
  - Marmot's own Airflow plugin only speaks the Airflow 2 API. Lineage therefore comes from Airflow's OpenLineage provider, and the other session later added an API bridge for Marmot's plugin.
  - Marmot reads the database as its own role, `marmot_reader`. Its plugin needs `pg_read_all_stats`, and that role would let the notebook role (`birddog_reader`) see other sessions' queries.

### 2.3 Location names repaired at the source (migration 022)
- **Decision.** 927 of the 1,201 `core.location` rows had double-encoded UTF-8 from the NocoDB import, e.g. `Korsunâ€™-Shevchenkivsâ€™kyy`. They are repaired by `core.repair_mojibake()`, which changes a value only when its bytes convert back cleanly. Both NocoDB import transforms call the same function, so a re-import stays clean.
- **Why.** The garbled names broke fuzzy matching and showed up on the maps.

---

## 3. Reading the archive books with Claude

### 3.1 Why books, and why Claude
- **The gap.** A book's description names one town. The towns people came from, and their names, are only on the handwritten pages. The catalog holds 43,497 multi-page PDFs, 9.1 million pages. The 19,720 vital-record books (birth, marriage, death and divorce registers) alone hold 7.0 million pages.
- **Why Claude.** It reads 19th-century Russian, Hebrew and Yiddish handwriting from page images, and it returns structured data.

### 3.2 Pipeline shape: probe, cost gate, read, assemble
- **Decision.** `read_books` runs four steps:
  1. **Probe.** Haiku looks at 3 sample pages and judges whether they are readable. It also measures tokens per page, which gives the cost estimate.
  2. **Cost gate.** A book estimated over **500k tokens** waits for a person, who sets `approved` or `rejected` in Mathesar.
  3. **Read.** Sonnet reads every page.
  4. **Assemble.** Haiku groups the spellings of each place, and a fuzzy match links them to `core.location`.

  Each run stops at its `budget_usd`. Every call's tokens and dollars are stored per book and per page.
- **Why.** The owner wanted "a simple pipeline that makes a decision": small books run automatically and big ones wait for approval. A cheap model decides and a strong model reads. A per-run budget means an estimate that turns out wrong can't run up a large bill.

### 3.3 Page images from Commons' renderer, not the PDFs
- **Decision.** Pages are fetched as rendered images from Commons, one page per request, about 1600 px wide.
- **Why.** The PDFs run to hundreds of MB, and the renderer needs nothing extra in the Airflow image.
- **Evidence.** We tested this against the original scans (3264×2448 px, about twice the resolution) on the worst page. Sharper images didn't help; the model and the prompt did. See 3.7.

### 3.4 The pilot: vital records only
- **Decision.** 20 vital-record books of 115 pages or fewer, $10 budget per run.
- **Result.** All 380 pages read. 425 places found, 323 of them (76%) matched to `core.location`. Cost $14.37.
- **Finding.** The cost came out 2.4 times the estimate, because Opus re-read 69% of the pages: it re-read any page where Sonnet was unsure of even one place.

### 3.5 Cost tuning: Opus only for illegible pages
- **Decision.** Opus now re-reads a page only when Sonnet marks it illegible (`REREAD_LOW_CONFIDENCE = False`). The estimate is calibrated on the pilot's measured tokens.
- **Evidence.** Place reading drops from $0.038 to **$0.013 a page**. The calibrated estimate lands at 1.01 times the actual cost (median, measured against the pilot).
- **Still open.** The 259 pilot pages that Opus re-read only for a single unsure place have not been checked for quality.

### 3.6 Full transcription, not just places
- **Decision.** The owner had expected the books to be transcribed in full. `transcribe_books` now stores, for every page:
  - the whole text in its original script, including pre-1918 letters and Hebrew
  - one record per register entry: event, date, person, parents, others and places
  - the places on the page

  The text is searchable in Postgres (`core.search_books`: full text with the 'simple' configuration, plus trigram fallback). There is a Grafana dashboard for it.
- **Why the 'simple' configuration.** The pages mix scripts and pre-reform spelling, so no language's stemmer fits.
- **Result.** 20 books and 380 pages, $10.05. 2,315 entries: 1,327 births, 764 deaths, 180 marriages, 7 divorces.

### 3.7 Names: Opus with a "best guess, mark the doubt" prompt
- **Problem.** 14% of entries had no readable name and 22% only part of one. Sonnet had written `[...]`.
- **Experiment.** On a 1855 death register page, the counts of readable names out of 11:

  | Variant | Readable names |
  |---|---|
  | Sonnet on the rendered page | 0 |
  | Sonnet on the original scan, halves or quarters | 0 |
  | Sonnet with a best-guess prompt | 0 |
  | **Opus with the best-guess prompt, original scan** ($0.17) | **11** |
  | **Opus with the best-guess prompt, 1600 px render** ($0.14) | **11** |
- **Decision.** Opus re-transcribes a page with that prompt when the page holds 2 or more, or 50% or more, entries with an unreadable name. The prompt asks for a best reading with each doubt marked `?`, checked against the Hebrew column. This rule picks 17% of pages.
- **Result on all 20 books.** Entries with an unreadable name fell from 332 to **66 (2.9%)**. 752 names carry `?` marks for volunteers to check. All-in transcription costs **$0.047 a page**.

### 3.8 Entry years from the book
- **Decision.** An entry that gives only a day and month takes the book's year, from the title or the cover page, and records `year_source`. A book that covers a range of years (e.g. 1846–1854) keeps the range rather than guessing a single year.
- **Result.** 1,375 of 2,315 entries now have a year.

---

## 4. Places: matching, positions and confirmation

### 4.1 Rough positions for unmatched places, with 3 suggestions
- **Decision.** For a place that didn't match `core.location`, Claude gives an approximate position with a confidence level and a reason. SQL then finds the 3 nearest known locations.
- **Result.** $0.17 for 146 of 205 rows. Most low-confidence guesses sit near the book's own town.
- **Why.** Volunteers can see roughly where a place is and get a shortlist to choose from. Typing the name into a gazetteer by hand would be slower.

### 4.2 Confirmation: per book, two people or one admin
- **Decision.** On the "Confirm places" page in Birddog, volunteers confirm a match, pick one of the suggestions, search, or answer "not a place" / "can't tell". A place counts as confirmed **for one book** when two people agree, or one admin decides. Only then is the book linked in `core.document_location`. Changing a confirmed answer removes the old link.
- **Why per book.** Common village names (Ольшанка) exist many times over. Confirming a name once for every book would link the wrong villages.
- **Why two people.** The data is crowd-checked, so a single mistaken click shouldn't change the catalog.
- **Search.** Typing in Cyrillic (Ukrainian, Russian or old spelling) is transliterated to the Latin names that `core.location` uses.

---

## 5. People and tools

- **Genizah** is the portal: one entry page and one login. Birddog accounts are synced to Airflow, Mathesar, Marmot, Saltcorn, ToolJet and Grafana, and JupyterHub accepts Birddog logins. A help page, `/help`, says what is where. Account creation is invite-only.
- **Grafana** shows maps and dashboards: Archive map, Books map, Collection overview, Book reading costs, Book transcriptions.
- **Mathesar** has curator and review views, e.g. `views.books__places`, `books__place_mentions`, `books__needs_approval`, `books__entries`.
- **JupyterHub** has example notebooks: `token_costs.ipynb` (cost explorer) and `book_places_review.ipynb`.
- **The web app was redesigned** with an archival theme and fonts that cover Cyrillic. The accessibility check went from 44 violations to 0. No routes or behaviour changed.

---

## 6. Deploying

### 6.1 Ansible run by GitHub Actions
- **Decision.** Deploys are Ansible playbooks (`deploy/ansible/`) run by `.github/workflows/deploy.yml` on GitHub-hosted runners. You start them by hand.
- **Safety.**
  - The default is a dry run.
  - The workflow ships exactly the pushed commit, so uncommitted work can't go out by accident.
  - One concurrency group means only one deploy runs at a time.
  - Tests run first.
  - A deploy that restarts Airflow or Postgres, or re-creates roles, runs in the `production-restart` environment, which waits for the owner's approval.
- **Why.**
  - The owner asked for deploys that don't need an AI session.
  - With the old scripts, two people deploying from one shared working tree could ship each other's unfinished work, and nothing stopped two deploys running at once.
  - The approval click enforces the existing rule that only the owner restarts postgres or airflow-*.
- **Credentials.** A dedicated ed25519 deploy key, created 2026-11-25. Its private half lives only in the GitHub secret `DEPLOY_SSH_KEY`. Rotation is described in `deploy/ansible/README.md`.
- **Transition.** `sh/deploy-data` and `sh/deploy-apps` keep working as the fallback until they are retired.

---

## 7. Costs

| Work | Per page | All 7.0M vital-record pages, standard | With the Batch API (half price) |
|---|---|---|---|
| Places only, tuned | $0.013 | about $92k | about $46k |
| Full transcription, with the Opus name re-read | $0.047 | about $330k | about $165k |

Spent so far, all on the 20-book pilot: places $14.37, transcription $10.05, name re-reads $7.91, rough positions $0.17.

**Next batch.** 100 vital-record books of 2–50 pages, both transcription and places, through the Batch API, with a $50 budget. The budget probably covers about 75–80 of the books, and the rest stay queued.

---

## 8. Open items

- **No off-site backup of the database.** The backup DAG exists, but its rclone remote isn't set up. This is the most urgent item.
- Review the pilot's places and names in the Confirm places page, then decide whether Opus re-reads of low-confidence places are worth paying for again (3.5).
- Credentials passed through chat on 2026-11-24 should be rotated: the Anthropic key, the apps-box account password and the Marmot keys.
- Set the Google Translate and Groq keys, or retire the Groq location finder now that Claude reads the books.
- The "Confirm places" list takes about 1 second per page at 650 cards. It will need a materialized view at a few thousand.
- Retire the sh deploy scripts once the GitHub workflow has run a few real deploys.
