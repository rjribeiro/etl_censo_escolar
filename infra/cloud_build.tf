 resource "google_cloudbuild_trigger" "update_dags" {
   name = "update-dags"
   github {
     owner = var.git_user
     name = "etl_censo_escolar"
     push {
       branch = "dev"
     }
   }

     substitutions = {
     _GCS_BUCKET = google_composer_environment.composer.config[0].dag_gcs_prefix
   }
   filename = "infra/update_dags.yaml"
 }


resource "google_cloudbuild_trigger" "update_scripts" {
  name = "update-etl-bucket"
  github {
    owner = var.git_user
    name = "etl_censo_escolar"
    push {
      branch = "dev"
    }
  }

    substitutions = {
    _GCS_BUCKET = google_storage_bucket.scripts.name
  }
  filename = "infra/update_scripts.yaml"
}