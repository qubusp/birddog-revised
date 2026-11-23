"""Builds the DAGs defined in dags/yaml/*.yml (dag-factory). Task code lives in
birddog/dag_tasks.py; the YAML only says what runs, when and in which order."""

from pathlib import Path

# The following import is here so Airflow parses this file
# from airflow import DAG
from dagfactory import load_yaml_dags

load_yaml_dags(globals_dict=globals(), dags_folder=str(Path(__file__).resolve().parent / "yaml"))
