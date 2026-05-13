# Deploying the Haiti Food Price Monitor to GCP

This guide walks through deploying the Streamlit dashboard at
[FEWS_Price_data/dashboard/app.py](../FEWS_Price_data/dashboard/app.py) to
**Google Cloud Run**, with the DuckDB price database stored in **Google Cloud
Storage (GCS)** and continuous deployment from `git push origin main` via
**Cloud Build**.

---

## 1. Architecture

```
                     ┌──────────────────────────┐
git push origin main │  GitHub repo (this one)  │
        │            └────────────┬─────────────┘
        ▼                         │ webhook
┌───────────────────┐             ▼
│   Cloud Build     │  reads cloudbuild.yaml + Dockerfile
│   (auto trigger)  │  builds image, pushes to GCR,
└────────┬──────────┘  redeploys Cloud Run service
         │
         ▼
┌───────────────────────────────────────────────┐
│  Cloud Run service: haiti-food-price-monitor  │
│   - Streamlit container, scale-to-zero         │
│   - On startup:  download DuckDB from GCS → /tmp
│   - On "🔄 Update Data & Models" click:        │
│        sync FEWS NET → /tmp DuckDB → push to GCS
└──────────────────────┬────────────────────────┘
                       │ read/write
                       ▼
┌───────────────────────────────────────────────┐
│  GCS bucket: gs://<project>-haiti-fews/        │
│      fews_haiti.duckdb   (~50 MB, persistent) │
└───────────────────────────────────────────────┘
```

### Why DuckDB-in-GCS and not Firestore / Cloud SQL?

- **DuckDB** is doing real analytical work (window functions, aggregations,
  Prophet feature prep) on ~68K time-series rows. Firestore is a document store
  optimized for indexed point reads — the wrong shape for this workload, and
  per-document-read pricing would dominate.
- **Cloud SQL Postgres** would work but costs ~$8/mo idle and is overkill for
  a single-writer, occasionally-updated, ~50 MB dataset.
- **DuckDB file in GCS** is ~$0.001/mo storage, and the container reads/writes
  it like any local file. Cloud Run's scale-to-zero keeps compute costs near $0
  at low traffic.

Expected total cost at light traffic: **<$5/mo, often $0**.

---

## 2. Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`).
- A GCP project with billing enabled. Set it as the active project:
  `gcloud config set project <PROJECT_ID>`.
- Required APIs enabled:
  ```bash
  gcloud services enable \
      run.googleapis.com \
      cloudbuild.googleapis.com \
      storage.googleapis.com \
      containerregistry.googleapis.com
  ```
- The local repo has been pushed to GitHub (Cloud Build trigger needs a
  GitHub source).

---

## 3. One-time setup

### 3a. Create the GCS bucket and seed it

```bash
PROJECT_ID=$(gcloud config get-value project)
BUCKET="${PROJECT_ID}-haiti-fews"

gcloud storage buckets create "gs://${BUCKET}" --location=us-central1

# Seed with the current local DuckDB so the first deploy doesn't have to
# do a full historical pull from FEWS NET (which can take ~5 min).
gcloud storage cp \
    FEWS_Price_data/database/fews_haiti.duckdb \
    "gs://${BUCKET}/fews_haiti.duckdb"
```

### 3b. Deploy the Cloud Run service for the first time

The first deploy uses `--source .` so Cloud Run builds the image from the
local repo. Subsequent deploys come from Cloud Build (next step).

```bash
gcloud run deploy haiti-food-price-monitor \
    --source . \
    --region us-central1 \
    --allow-unauthenticated \
    --memory 2Gi \
    --cpu 2 \
    --timeout 600 \
    --set-env-vars "GCS_BUCKET=${BUCKET},GCS_BLOB_NAME=fews_haiti.duckdb,FEWS_DB_PATH=/tmp/fews_haiti.duckdb"
```

Memory is set to 2 GiB because Prophet model fitting across all markets can
spike past 1 GiB. Timeout 600s covers a full retrain.

### 3c. Grant the Cloud Run service account read/write on the bucket

```bash
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${RUN_SA}" \
    --role=roles/storage.objectAdmin
```

(If you bound a custom service account to the Cloud Run service, substitute
that one for `RUN_SA`.)

### 3d. Wire up Cloud Build for push-to-deploy

1. In the GCP console, go to **Cloud Build → Triggers → Connect Repository**
   and connect the GitHub repo (one-time OAuth).
2. Create the trigger:
   ```bash
   gcloud builds triggers create github \
       --repo-name=haiti_fews_forecast \
       --repo-owner=mmann1123 \
       --branch-pattern='^main$' \
       --build-config=cloudbuild.yaml \
       --name=haiti-fews-deploy
   ```
3. Grant the Cloud Build service account the roles it needs to deploy:
   ```bash
   CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

   gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
       --member="serviceAccount:${CB_SA}" \
       --role=roles/run.admin

   gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
       --member="serviceAccount:${CB_SA}" \
       --role=roles/iam.serviceAccountUser
   ```

That's it — the next `git push origin main` triggers a full build + deploy.

---

## 4. Environment variables

| Variable        | Purpose                                                                 | Example                                         | Consumed in                                                                                          |
| --------------- | ----------------------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `FEWS_DB_PATH`  | Where the DuckDB file lives on the container's writable disk.           | `/tmp/fews_haiti.duckdb`                        | `FEWS_Price_data/database/fews_database.py`, `FEWS_Price_data/dashboard/app.py`, pricing modules     |
| `GCS_BUCKET`    | Bucket name (no `gs://` prefix). When unset, GCS sync is skipped.       | `my-project-haiti-fews`                         | `FEWS_Price_data/dashboard/app.py` (`_bootstrap_db_from_gcs`, `_push_db_to_gcs`)                     |
| `GCS_BLOB_NAME` | Object name within the bucket. Defaults to `fews_haiti.duckdb`.         | `fews_haiti.duckdb`                             | same as above                                                                                         |

When `GCS_BUCKET` is unset (typical local dev), the app uses the local DuckDB
file at `FEWS_Price_data/database/fews_haiti.duckdb` and never touches GCS.

---

## 5. Daily workflow

```
edit code locally
       │
       ▼
git commit -am "<change>"
       │
       ▼
git push origin main
       │
       ▼  Cloud Build trigger fires
       ▼
docker build → push to GCR → gcloud run deploy
       │
       ▼  ~3-5 min later
       ▼
new revision live at https://haiti-food-price-monitor-<hash>-uc.a.run.app
```

### Watch a build in progress

```bash
gcloud builds list --ongoing
gcloud builds log --stream <BUILD_ID>
```

### Roll back to the previous Cloud Run revision

```bash
gcloud run revisions list --service=haiti-food-price-monitor --region=us-central1
gcloud run services update-traffic haiti-food-price-monitor \
    --region=us-central1 \
    --to-revisions=<PREVIOUS_REVISION>=100
```

---

## 6. The "🔄 Update Data & Models" button (in production)

Click flow when running on Cloud Run:

1. The sidebar handler in [app.py](../FEWS_Price_data/dashboard/app.py) calls
   `_incremental_sync()`, which fetches fresh records from the FEWS NET API
   (public, no auth) and writes them to `/tmp/fews_haiti.duckdb`.
2. After a successful sync, `_push_db_to_gcs()` uploads the updated DuckDB
   file back to the bucket.
3. `st.cache_data.clear()` invalidates Streamlit's query cache and
   `st.session_state.forecast_models = {}` forces Prophet to retrain on the
   next forecast tab visit.

**Why the upload step is non-negotiable:** Cloud Run scales to zero. Once the
container is recycled, anything written to `/tmp` is gone. Without the upload,
your sync just trained Prophet against soon-to-be-discarded data. If the
upload fails (e.g., transient permission issue), the sidebar shows a yellow
warning — re-click the button to retry once the issue is resolved.

Verify in logs:

```bash
gcloud run services logs read haiti-food-price-monitor \
    --region=us-central1 --limit=50
```

---

## 7. Cost monitoring

- Billing console: <https://console.cloud.google.com/billing>
- Expected: <$5/mo at light traffic. Most of it is GCS storage ($0.02/GB/mo)
  and Cloud Build minutes (free for the first 120/day; a typical build
  here is ~4 min).
- Things that push cost up:
  - Setting `--min-instances=1` on Cloud Run (~$15/mo to keep one warm).
  - Frequent rebuilds (each push consumes ~4 build-minutes).
  - A bloated bucket — only the active DuckDB file should live there.

---

## 8. Troubleshooting

| Symptom                                                                   | Likely cause                                                            | Fix                                                                                                  |
| ------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Cold start hangs ~30s before showing UI                                   | Cloud Run is downloading `fews_haiti.duckdb` from GCS                  | Normal at scale-to-zero. Set `--min-instances=1` if it bothers you (paid).                          |
| App boots, sidebar shows "Could not download DuckDB from gs://..."        | Service account lacks bucket access, or bucket/blob name is wrong       | Re-run the IAM binding in §3c. Confirm `GCS_BUCKET` env var matches the bucket.                     |
| Prophet retrain crashes container ("OOMKilled" in logs)                   | 2 GiB not enough for all markets at once                                | Bump memory: `gcloud run services update haiti-food-price-monitor --region=us-central1 --memory=4Gi`. |
| "Update Data & Models" succeeds but data is gone after a few min idle     | Upload to GCS failed silently, or env var was unset on the revision    | Check logs for the warning toast text. Verify `GCS_BUCKET` is set on the active revision.            |
| `git push` doesn't trigger a build                                        | Trigger not connected, or branch pattern doesn't match                  | `gcloud builds triggers list`; check the GitHub repo's webhook in the connected repository settings. |
| Cloud Build fails with `permission denied` deploying to Run               | Cloud Build SA missing `run.admin` / `iam.serviceAccountUser`           | Re-run the bindings in §3d.                                                                          |

---

## 9. Local development against the deployed bucket

To debug against real production data without a fresh sync:

```bash
# 1. Create a service-account key (only for local dev; never commit)
gcloud iam service-accounts create haiti-fews-local --display-name "Local dev"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:haiti-fews-local@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role=roles/storage.objectAdmin
gcloud iam service-accounts keys create ~/keys/haiti-fews-local.json \
    --iam-account="haiti-fews-local@${PROJECT_ID}.iam.gserviceaccount.com"

# 2. Point the app at GCS
export GOOGLE_APPLICATION_CREDENTIALS=~/keys/haiti-fews-local.json
export GCS_BUCKET="${PROJECT_ID}-haiti-fews"
export GCS_BLOB_NAME=fews_haiti.duckdb
export FEWS_DB_PATH=/tmp/fews_haiti.duckdb

# 3. Run the dashboard
streamlit run FEWS_Price_data/dashboard/app.py
```

⚠️ Coordinate with anyone else hitting the live bucket — clicking "Update Data
& Models" locally will overwrite the production DuckDB. For read-only
debugging, copy the blob to a different filename first:

```bash
gcloud storage cp \
    "gs://${GCS_BUCKET}/fews_haiti.duckdb" \
    "gs://${GCS_BUCKET}/fews_haiti.local-debug.duckdb"
export GCS_BLOB_NAME=fews_haiti.local-debug.duckdb
```


---

## 10. ACLED data refresh (monthly Cloud Run Job)

The Streamlit service handles **price** data on demand from the dashboard.
Conflict data from ACLED is refreshed on a separate, automated schedule via
a Cloud Run **Job** triggered by Cloud Scheduler.

### 10a. What it produces

Each run:

1. Downloads the canonical DuckDB from `gs://${BUCKET}/fews_haiti.duckdb`.
2. Pulls new ACLED events for Haiti since `MAX(event_date)` in `acled_events`
   (re-pulling the last 7 days to catch ACLED's late corrections).
3. Upserts events, rebuilds `acled_features_national` and
   `acled_features_market` from scratch.
4. Exports two CSVs:
   - `gs://${BUCKET}/acled_national.csv` — the contract for the R/NIMBLE
     model (`fit_ar_sv.R`'s `ACLED_PATH` should point here).
   - `gs://${BUCKET}/acled_by_market.csv` — long format, future-proofing for
     per-market modeling.
5. Uploads the updated DuckDB back to GCS.

The R model lives in a separate repo and consumes `acled_national.csv` on
its own cadence — this job's contract stops at "CSVs in GCS".

### 10b. One-time setup

```bash
PROJECT_ID=$(gcloud config get-value project)
BUCKET="${PROJECT_ID}-haiti-fews"
REGION=us-central1

# 1. Store myACLED credentials in Secret Manager (OAuth2 password grant)
echo -n "<your-myacled-username>" | gcloud secrets create acled-username \
    --replication-policy=automatic --data-file=-
echo -n "<your-myacled-password>" | gcloud secrets create acled-password \
    --replication-policy=automatic --data-file=-

# 2. Build + deploy the job via Cloud Build
gcloud builds submit \
    --config=cloudbuild.acled-job.yaml \
    --substitutions=_REGION=${REGION},_GCS_BUCKET=${BUCKET}

# 3. Grant the job's runtime SA access to the secrets + GCS bucket
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
JOB_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for SECRET in acled-username acled-password; do
  gcloud secrets add-iam-policy-binding "${SECRET}" \
      --member="serviceAccount:${JOB_SA}" \
      --role=roles/secretmanager.secretAccessor
done

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${JOB_SA}" \
    --role=roles/storage.objectAdmin

# 4. Backfill: full historical pull (one-time, ~30s)
gcloud run jobs execute haiti-acled-refresh \
    --region=${REGION} \
    --args="sync,--full,--start=2018-01-01" \
    --wait

# 5. Schedule the monthly incremental refresh (5th of each month at 06:00 UTC)
gcloud scheduler jobs create http monthly-acled-refresh \
    --location=${REGION} \
    --schedule="0 6 5 * *" \
    --time-zone=Etc/UTC \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/haiti-acled-refresh:run" \
    --http-method=POST \
    --oauth-service-account-email="${JOB_SA}"
```

### 10c. Environment variables and secrets (job runtime)

| Variable        | Source           | Purpose                                              |
| --------------- | ---------------- | ---------------------------------------------------- |
| `ACLED_USERNAME`| Secret Manager   | `acled-username:latest` (myACLED account username)   |
| `ACLED_PASSWORD`| Secret Manager   | `acled-password:latest` (myACLED account password)   |
| `GCS_BUCKET`    | env var (deploy) | Same bucket as the Streamlit service                 |
| `GCS_BLOB_NAME` | env var (deploy) | `fews_haiti.duckdb`                                  |
| `FEWS_DB_PATH`  | env var (deploy) | `/tmp/fews_haiti.duckdb`                             |
| `ACLED_CSV_DIR` | env var (deploy) | `/tmp` — where CSVs land before upload               |

### 10d. Manual runs

```bash
# Trigger the next scheduled run immediately
gcloud scheduler jobs run monthly-acled-refresh --location=${REGION}

# Ad-hoc execution with overrides (e.g., full re-pull)
gcloud run jobs execute haiti-acled-refresh \
    --region=${REGION} \
    --args="sync,--full,--start=2018-01-01" \
    --wait

# Inspect the most recent run
gcloud run jobs executions list \
    --job=haiti-acled-refresh --region=${REGION} --limit=5
gcloud run jobs executions logs <EXECUTION_NAME> --region=${REGION}
```

### 10e. Cost

- Cloud Run Job: ~30-60s/month × 1 vCPU × 1 GiB ≈ **<$0.01/month**
- Cloud Scheduler: first 3 jobs free; this is free.
- Secret Manager: 6 access ops/year ≈ free tier.
- Net additional spend on top of §7's <$5/month: effectively zero.
