### Before you start
You need a **workspace URL** and a **personal access token** for both the old and the new workspace.

---

### Where to find each value

**Workspace URL**
Open the workspace in your browser and copy the address up to `.net` or `.com`, for example `https://adb-1234567890123456.7.azuredatabricks.net`.

**Access token**
1. In the workspace, click your username (top right) → **Settings**.
2. **Developer** → **Access tokens** → **Manage** → **Generate new token**.
3. Give it a comment and a lifetime (for example 30 days) → **Generate**.
4. **Copy the token now.** It's shown only once.

If token creation is disabled, a workspace admin must allow it (Admin settings → Advanced → Personal access tokens).

**Advanced (optional)**
- **Folder to migrate**: leave empty to copy your home folder (`/Users/<your email>`).
- **Cluster ID for jobs** (new workspace): if jobs used a specific cluster, create one in the new workspace and paste its ID here (Compute → open the cluster → the ID is in the URL or under Advanced options → Tags).

---

### What isn't copied automatically
- **Table data** (Delta / Unity Catalog): write the tables to your own cloud storage before the old workspace closes.
- **Secret values**: the app lists every scope and key with the commands to recreate them.
- **Clusters**: their settings are saved, and the app gives a create command. They aren't started automatically, so nothing costs money by surprise.

---

### Common errors

| Message contains | Fix |
|---|---|
| `401` / `Invalid access token` | Generate a new token and paste it again |
| `403` / `PERMISSION_DENIED` | The token's user lacks rights. Use a workspace admin's token |
| `Could not resolve host` | Check the workspace URL |
