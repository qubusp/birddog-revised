# Loaded by start.sh (JupyterLab) and export.py (CLI export).
# Elyra resolves each Airflow operator's import line from this list. Its
# built-in default still names Airflow 1/2 modules (airflow.operators.bash_operator)
# that do not exist in Airflow 3, so point the names at providers-standard,
# which is what the server runs (Airflow 3.3.2 + apache-airflow-providers-standard 1.19.0).
c.AirflowPipelineProcessor.available_airflow_operators = [
    "airflow.providers.standard.operators.bash.BashOperator",
    "airflow.providers.standard.operators.trigger_dagrun.TriggerDagRunOperator",
]
