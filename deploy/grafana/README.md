# Grafana: maps and dashboards

Grafana OSS 13 on the apps box at `https://grafana.<PUBLIC_DOMAIN>`, linked
from Genizah. It reads the `birddog` database over WireGuard as the read-only
role `birddog_reader` (the password is `JUPYTER_DB_PASSWORD`, shared with
JupyterHub). Grafana's own state (users, preferences) is SQLite in the
`grafana-data` volume.

| Dashboard | File | Shows |
|---|---|---|
| Archive map (home) | `dashboards/archive-map.json` | Every location as a slate dot on a light grey map, with the places the books name on top, sized and coloured by mentions, with region, 1900 district/province and the Jewish population note |
| Books map | `dashboards/books-map.json` | Places read in the books (`read_books`) and matched to a location; most-named places; unmatched places |
| Collection overview | `dashboards/collection.json` | Pages, documents, scan pages, translations, FTP matching, documents per archive, work queues |
| Book reading: progress and costs | `dashboards/books-costs.json` | Books by status, dollars and tokens per book, model and day |
| Book transcriptions | `dashboards/transcriptions.json` | Search box over the transcripts (`core.search_books`), register entries by event and year, people per place on the map, progress and cost per book (views from migration 023) |

The data behind the maps comes from migration 021: `core.location` gets
`latitude`, `longitude`, `region` and `jewish_population` from the NocoDB raw
records, and the views `views.map__locations`, `views.map__book_places` and
`views.stats__books`. `core.document_location` is still empty, so the
"documents" counts on the archive map stay 0 until documents are linked to
places.

**Maps** use Esri's World Light Gray Canvas tiles (no API key; CARTO now needs
one) and the theme's accents, ochre to stamp red; keep green off the maps.

**Making dashboards.** `make_dashboards.py` writes the JSON files; edit it and
rerun it rather than hand-editing JSON, so panels stay consistent.

**Editing dashboards.** Admins can edit and save the provisioned dashboards
in Grafana (`allowUiUpdates`), but the JSON files in the repo win: when a file
changes and is deployed, Grafana replaces the saved version with it. To keep
an edit for good, *Export → Save to file*, commit the JSON over the old file
and deploy the apps box. Grafana reloads the files without a restart.

**Accounts.** Birddog accounts are copied in by `birddog/user_sync.py`
(Birddog admin → Admin and server admin, user → Viewer), with the Birddog password. The
break-glass admin is `admin` / `GRAFANA_ADMIN_PASSWORD` in the apps box
`deploy/.env` (made by `sh/deploy-apps`).

Restart without asking: `docker compose up -d --no-deps grafana`.
