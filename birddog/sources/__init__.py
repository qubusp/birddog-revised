"""Source kit: onboard a data source as one folder, dags/sources/<name>/.

  spec.py      source.yml schema and validation
  fetchers.py  built-in fetchers (http_json, csv, xlsx, gsheet, sftp/ftp listing, html)
  runner.py    land -> transform -> verify -> expose against Postgres
  dags.py      one Airflow DAG per source (dags/load_sources.py calls it)
  scaffold.py  `python -m birddog.sources new <name> --type <type>`

Guide: dags/sources/README.md. Landing tables: deploy/db/migrations/013_source_kit.sql."""
