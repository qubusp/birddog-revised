"""Builds one Airflow DAG per data source in dags/sources/<name>/source.yml
(the source kit, birddog/sources; how to add a source: dags/sources/README.md).
Each DAG is source_<name>: land -> transform -> verify -> expose."""

from pathlib import Path

# The following import is here so Airflow parses this file
# from airflow import DAG
from birddog.sources.dags import build_dags

globals().update(build_dags(Path(__file__).resolve().parent / "sources"))
