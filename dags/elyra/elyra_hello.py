from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

import pendulum

args = {
    "project_id": "elyra_hello",
}

dag = DAG(
    dag_id="elyra_hello",
    default_args=args,
    schedule=None,
    start_date=pendulum.today("UTC").add(days=-1),
    catchup=False,
    description="""
Proof pipeline: two BashOperator nodes authored in Elyra, exported to dags/elyra/.
    """,
    is_paused_upon_creation=True,
)


# Operator source: {"catalog_type": "local-directory-catalog", "component_ref": {"path": "bash.py"}}

op_say_hello = BashOperator(
    task_id="say_hello",
    bash_command="echo hello from Elyra",
    executor_config={},
    dag=dag,
)


# Operator source: {"catalog_type": "local-directory-catalog", "component_ref": {"path": "bash.py"}}

op_python_step = BashOperator(
    task_id="python_step",
    bash_command="python -c \"import sys, platform; print('step 2 on python', platform.python_version())\"",
    executor_config={},
    dag=dag,
)

op_python_step << op_say_hello
