# Cloud Account Migration

Copies everything from an **expiring cloud account** (for example a free trial) into a **new account**.
It backs the old account up to your own computer, rebuilds it in the new account, then checks that both match.

Works today for **Snowflake**, **Azure** (Storage + Data Factory) and **Databricks**. New platforms are added as plugins (see the end of this page).

---

## Quick start (Windows)

1. **Install Python 3.12** from python.org. During setup, tick **"Add python.exe to PATH"**.
   (Avoid Python 3.15 or newer for now; some libraries don't support it yet.)
2. **Unzip** this folder somewhere simple, for example `C:\CloudMigration`.
3. **Double-click `Start.bat`.**
   The first time, it sets itself up (2–5 minutes). After that it opens in seconds.
   The app opens in your browser. Keep the black window open while you use it; close it to stop.

Mac / Linux: run `bash start.sh` instead.

## First time with a platform?

On the **Accounts** page, click **How do I get these details?**. A pop-up shows, step by step:
- where to find every value
- what you must create first (for Azure: an **app registration**, its **client secret**, and the **role** it needs)
- fixes for the most common errors

The same guides are in the `guides` folder.

## Using the app

| Step | What you do |
|---|---|
| **1. Accounts** | Pick the platform. Fill in the **old account** and the **new account**, then click **Test connection** for each. Click **Save profile** to reuse the details next time. |
| **2. Back up** | Click **Scan old account**, untick anything you don't want, then click **Back up now**. The backup is saved on your computer in `Migration_Backups`. |
| **3. Restore** | Pick the backup, tick the confirmation box, then click **Restore now**. The list under "Things you still need to do" shows the few manual steps (for example approving an Azure link). |
| **4. Check** | Click **Run check**. Every object, row count and a fingerprint of every table's data are compared. Green means it matches. |
| **Backups & reports** | Read any past report, or open the backup folder. |

You can back up today and restore days later, even after the old account has closed.

### Where things are stored
- **Backups:** `Migration_Backups\<platform>_<date_time>` inside this folder. Keep them; they are your offline copy.
- **Saved profiles:** `profiles\` (account names, users and settings).
- **Passwords and tokens:** your computer's credential store (Windows Credential Manager or macOS Keychain). They are never written to a file.

### Tips
- **Don't rename or move the folder** after the first run. If you must, delete the `.venv` folder and run `Start.bat` again.
- To **update the tool**, unzip the new version over this folder and choose **Replace**. Your backups, profiles and setup are kept.
- Restore into a **new or empty account**. Existing objects with the same names are replaced.

---

## What is copied

| Platform | Copied automatically | You finish by hand (the app tells you) |
|---|---|---|
| **Snowflake** | Databases, schemas, tables **with data**, views, materialized/dynamic/external tables, file formats, sequences, procedures, functions, streams, tasks, pipes, stages and every file in internal stages, warehouses, resource monitors, account settings, roles and grants, users and role assignments, policies, tags, alerts, storage integrations, notebooks and Streamlit apps, **all Workspaces worksheets**, and your **query history** (last 365 days) | Approve the Azure consent link for storage integrations, resume tasks/alerts, set passwords for other users, re-create secrets, network policies and shares |
| **Azure** | Storage containers and files (folder paths kept), Data Factory linked services, datasets, data flows, pipelines, triggers | Re-enter linked-service passwords/keys (Azure never exports them), start triggers |
| **Databricks** | Notebooks and files (your `/Users/<email>` paths are fixed automatically), jobs (created paused), pipelines, cluster settings (saved only) | Table data, secrets, re-cloning Git folders, creating clusters |

**Nothing can copy these** (the platform creates them itself): query, login and billing history inside the account, Time Travel versions, pending changes inside streams, load history, password and secret values, original creation dates, trial credits, and Snowsight dashboards (their SQL is in the query-history backup).

---

## Command line (optional, for advanced users)

Everything the app does is also available from a terminal, using settings from a `.env` file (copy `.env.example` to `.env`):

```
.venv\Scripts\activate
python migrate.py backup snowflake
python migrate.py restore  Migration_Backups\snowflake_<date_time>
python migrate.py restore  Migration_Backups\snowflake_<date_time> --only workspaces
python migrate.py validate Migration_Backups\snowflake_<date_time>
```

Use `python -m pip ...` rather than `pip ...` inside this folder.

## Adding another platform (AWS, GCP, BigQuery, Postgres …)

1. Copy `connectors/_template.py` to `connectors/<platform>_connector.py`.
2. List its login fields in `fields = [...]`. The app builds the login form from this list automatically.
3. Implement `connect`, `discover`, `backup`, `restore` and `validate`.
4. Add one line to `core/registry.py`.

The app, backups, checksums, reports and checks then work for it with no other changes.

## Tests

`python -m pytest tests` runs the engine, a simulated Snowflake migration, and a full click-through of the app using a demo platform. No cloud account is needed.
