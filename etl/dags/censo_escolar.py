from datetime import datetime
import json
import re
from math import ceil

from airflow import DAG
from airflow.utils.dates import days_ago
from airflow.utils.task_group import TaskGroup
from airflow.models import Variable
from airflow.operators.dummy import DummyOperator
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.providers.google.cloud.operators.kubernetes_engine import (
    GKEStartPodOperator,
    GKECreateClusterOperator,
    GKEDeleteClusterOperator
)
from airflow.providers.google.cloud.operators.dataproc import (
    DataprocInstantiateWorkflowTemplateOperator,
    DataprocCreateWorkflowTemplateOperator
)
from kubernetes.client import V1ResourceRequirements
from google.cloud import storage


PROJECT = Variable.get("PROJECT")
FIRST_YEAR = int(Variable.get("CENSO_ESCOLAR_FIRST_YEAR"))
LAST_YEAR = int(Variable.get("CENSO_ESCOLAR_LAST_YEAR"))

LANDING_BUCKET = f"{PROJECT}-landing"
PROCESSING_BUCKET = f"{PROJECT}-processing"
CONSUMER_BUCKET = f"{PROJECT}-consumer"
SCRIPTS_BUCKET = f"{PROJECT}-scripts"
YEARS = list(range(FIRST_YEAR, LAST_YEAR + 1))

NOW = datetime.now().isoformat()


def check_years(**context):
    ti = context["ti"]
    true_option = context["true_option"]
    false_option = context["false_option"]
    client = storage.Client()
    bucket = client.get_bucket(context["bucket"])
    years_in_this_bucket = set([int(re.findall("([0-9]{4})\/", blob.name)[0])
                                for blob in list(bucket.list_blobs(prefix="censo-escolar"))
                                if re.findall("([0-9]{4})\/", blob.name)])
    years_not_in_this_bucket = set(context["years"]) - years_in_this_bucket
    if years_not_in_this_bucket:
        ti.xcom_push(key="years_not_in_this_bucket",
                     value=json.dumps(list(years_not_in_this_bucket)))
        ti.xcom_push(key="years_not_in_this_bucket_str",
                     value=" ".join(map(str, years_not_in_this_bucket)))
        ti.xcom_push(key="cluster_size",
                     value=calculate_cluster_size(len(years_not_in_this_bucket)))
        return true_option
    else:
        return false_option


def check_year(**context):
    ti = context["ti"]
    year = context["year"]
    true_option = context["true_option"]
    false_option = context["false_option"]
    years_not_in_this_bucket = ti.xcom_pull(task_ids=context["task"],
                                            key="years_not_in_this_bucket")
    if year in json.loads(years_not_in_this_bucket):
        return true_option
    else:
        return false_option


def calculate_cluster_size(amount_years):
    return ceil(int(amount_years)/2) + 1


def get_gke_cluster_def():
    cluster_def = {
        "name": "censo-escolar-extraction",
        "initial_node_count": '{{ ti.xcom_pull(task_ids="extract.check_landing_bucket", key="cluster_size") }}',
        "location": "southamerica-east1-a",
        "node_config": {
            "oauth_scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            "machine_type": "e2-standard-4"
        },
    }
    return cluster_def


def get_pod_resources():
    return V1ResourceRequirements(
        requests={
            "cpu": "1.7",
            "memory": "4G"
        },
        limits={
            "cpu": "1.7",
            "memory": "4G"
        }
    )


def get_dataproc_workflow():
    workflow = {
        "id": f"censo-escolar-transform-{NOW}",
        "name": "censo-transform",
        "placement": {
            "managed_cluster": {
                "cluster_name": "censo-escolar-transform",
                "config": {
                    "master_config": {
                        "num_instances": 1,
                        "machine_type_uri": "n1-highmem-8"
                    },
                    "worker_config": {
                        "num_instances": 2,
                        "machine_type_uri": "n1-highmem-8"
                    },
                    "gce_cluster_config": {
                        "zone_uri": "us-east1-b"
                    }
                }
            },
        },
        "jobs": []
    }

    prev_job = None
    jobs = []
    #years = "{{ ti.xcom_pull(task_ids='check_processing_bucket', key='years_not_in_this_bucket_str'}}"
    years = '{{ (ti.xcom_pull(task_ids="check_processing_bucket", key="years_not_in_this_bucket_str") | fromjson) }}'
    for year_ in years:
        step_id = f"censo-transform-{year_}",
        job = {
            "sted_id": step_id,
            "pyspark_job": {
                "main_python_file_uri": f"gs://{PROCESSING_BUCKET}/censo_escolar/transformation/transform.py",
                "args": [PROJECT, year_]
            }
        }

        if prev_job:
            job["prerequisite_step_ids"] = prev_job

        prev_job = step_id
        jobs.append(job)

    workflow["jobs"] = jobs

    return workflow


with DAG(dag_id="censo-escolar",
         default_args={'owner': 'airflow'},
         start_date=days_ago(0),
         user_defined_filters={'fromjson': lambda s: json.loads(s)}) as dag:
    with TaskGroup(group_id="extract") as extract:
        check_landing_bucket = BranchPythonOperator(
            task_id="check_landing_bucket",
            python_callable=check_years,
            provide_context=True,
            op_kwargs={
                "true_option": 'extract.create_gke_cluster',
                "false_option": "extract.extraction_finished_wih_sucess",
                "bucket": f"{PROJECT}-landing",
                "years": YEARS
            }
        )

        create_gke_cluster = GKECreateClusterOperator(
            task_id='create_gke_cluster',
            project_id=PROJECT,
            location="southamerica-east1-a",
            body=get_gke_cluster_def()
        )

        with TaskGroup(group_id="download") as download:
            for year in YEARS:
                check_before_download = BranchPythonOperator(
                    task_id=f"check_before_download_year_{year}",
                    python_callable=check_year,
                    provide_context=True,
                    op_kwargs={
                        "true_option": f"extract.download.download_year_{year}",
                        "false_option": f"extract.download.download_year_{year}_finished",
                        "year": year,
                        "task": "extract.check_landing_bucket"
                    }
                )

                download_year = GKEStartPodOperator(
                    task_id=f"download_year_{year}",
                    project_id=PROJECT,
                    location="southamerica-east1-a",
                    cluster_name="censo-escolar-extraction",
                    namespace="default",
                    image=f"gcr.io/{PROJECT}/censo_escolar_extraction:latest",
                    arguments=["sh", "-c", f'python extract.py {year} {LANDING_BUCKET}'],
                    resources=get_pod_resources(),
                    name=f"extract-file-{year}",
                    get_logs=True,
                    startup_timeout_seconds=600
                )

                download_year_finished = DummyOperator(
                    task_id=f"download_year_{year}_finished",
                    trigger_rule="all_success"
                )

                check_before_download >> download_year >> download_year_finished
                check_before_download >> download_year_finished

        destroy_gke_cluster = GKEDeleteClusterOperator(
            task_id="destroy_gke_cluster",
            name="censo-escolar-extraction",
            project_id=PROJECT,
            location="southamerica-east1-a",
            trigger_rule="all_done"
        )

        extraction_finished_wih_sucess = DummyOperator(
            task_id="extraction_finished_wih_sucess",
            trigger_rule='none_failed'
        )

        check_landing_bucket >> [create_gke_cluster, extraction_finished_wih_sucess]
        create_gke_cluster >> download >> [destroy_gke_cluster, extraction_finished_wih_sucess]

    with TaskGroup(group_id="transform") as transform:
        check_processing_bucket = BranchPythonOperator(
            task_id="check_processing_bucket",
            python_callable=check_years,
            provide_context=True,
            op_kwargs={
                "true_option": "transform.create_workflow_template",
                "false_option": "transform.transformation_finished_with_sucess",
                "bucket": PROCESSING_BUCKET,
                "years": YEARS
            },
            trigger_rule="none_failed"
        )

        create_workflow_template = DataprocCreateWorkflowTemplateOperator(
            task_id="create_workflow_template",
            template=get_dataproc_workflow(),
            project_id=PROJECT,
            location="us-east1",
        )

        run_dataproc_job = DataprocInstantiateWorkflowTemplateOperator(
            task_id=f"run_dataproc_job",
            template_id=f"censo-escolar-transform-{NOW}",
            project_id=PROJECT,
            region="us-west1"
        )

        transformation_finished_with_sucess = DummyOperator(
            task_id="transformation_finished_with_sucess",
            trigger_rule='none_failed'
        )

        check_processing_bucket >> [create_workflow_template, transformation_finished_with_sucess]
        create_workflow_template >> run_dataproc_job >> transformation_finished_with_sucess

    extract >> transform