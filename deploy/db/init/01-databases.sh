#!/bin/bash
# Runs once, when the Postgres volume is first created.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres <<EOSQL
CREATE ROLE airflow LOGIN PASSWORD '${AIRFLOW_DB_PASSWORD}';
CREATE DATABASE airflow OWNER airflow;

CREATE ROLE birddog LOGIN PASSWORD '${BIRDDOG_DB_PASSWORD}';
CREATE DATABASE birddog OWNER birddog;
EOSQL
