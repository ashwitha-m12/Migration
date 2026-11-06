### Before you start (first time only, about 15 minutes)

The tool copies two things, and each needs its own details:

| Part | What it copies | What the tool needs |
|---|---|---|
| **Storage** | Containers and files | One **connection string** per storage account |
| **Data Factory** | Pipelines, datasets, linked services, data flows, triggers | Subscription ID, Tenant ID, Resource group, Factory name, and an **app login** (Client ID + Client secret) |

You can fill in only the part you need. Leave **Subscription ID** empty to skip Data Factory, or leave **Storage connection string** empty to skip storage.

---

### 1. Storage connection string
Do this for the old storage account and again for the new one.

1. Azure Portal → search **Storage accounts** → open the account.
2. Left menu: **Security + networking → Access keys**.
3. Next to **key1 → Connection string**, click **Show**, then the copy icon.
4. Paste it into **Storage connection string**.

**One connection string = one storage account.** If your containers are spread over several storage accounts, back each one up separately: change the connection string and click **Back up now** again. Each run creates its own backup folder.

**Creating the new storage account?**
- If the old account used **Data Lake** (addresses ending in `.dfs.core.windows.net`, or "Hierarchical namespace: Enabled" on its Overview page), tick **Enable hierarchical namespace** on the **Advanced** tab when you create the new one. It can't be switched on cleanly later.
- On the **Data protection** tab, untick **soft delete for blobs** and **for containers** if you might run the restore more than once. Otherwise every re-run keeps hidden old copies.

---

### 2. Subscription ID, Tenant ID, Resource group, Factory name
- **Subscription ID**: Portal → **Subscriptions** → copy the **Subscription ID** of the subscription that holds the factory.
- **Tenant ID**: Portal → **Microsoft Entra ID** → **Overview** → **Tenant ID**.
- **Resource group** and **Factory name**: Portal → **Data factories** → open the factory → **Overview** shows both.

For the **new** account, the resource group and factory can be new names; the tool creates them. Fill in **Region for new resources** (for example `westeurope`), or leave it empty to use the old factory's region.

---

### 3. App login: Client ID and Client secret (recommended)
Data Factory needs a login the tool can use. Create one **app registration** in each Azure account (tenant): one in the old, one in the new. If both are in the same tenant, one app is enough.

1. Portal → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name it, for example `cloud-account-migration`, leave the defaults, and click **Register**.
3. On the app's **Overview**, copy:
   - **Application (client) ID** → paste into **Service principal client ID**
   - **Directory (tenant) ID** → paste into **Tenant ID**
4. Left menu: **Certificates & secrets** → **Client secrets** → **New client secret** → add a description → **Add**.
5. **Copy the `Value` column immediately.** It's shown only once. Paste it into **Service principal secret**. Don't use the "Secret ID" column; that's a different value.

> Without a Client ID, the tool falls back to the **Azure CLI** (`az login`). That only works if the Azure CLI is installed and signed in to the right account, so the app login is easier.

---

### 4. Give the app permission (the step most people miss)
An app registration starts with **no access at all**. Assign it a role:

| Account | Role to give the app | Where |
|---|---|---|
| **Old** | **Reader** | On the resource group that holds the factory |
| **New** | **Contributor** | On the **subscription** (so the tool can create the resource group), or on the resource group if it already exists |

Steps (the same for both):
1. Open the **subscription** or **resource group** → **Access control (IAM)** → **+ Add** → **Add role assignment**.
2. Find the role. **Contributor is under the "Privileged administrator roles" tab**; Reader is under "Job function roles". Select it → **Next**.
3. **Assign access to: User, group, or service principal** → **+ Select members** → search for your app's name (or paste its Client ID) → **Select**.
4. **Review + assign** (click it twice).
5. Wait **1–2 minutes** before testing again.

You need to be **Owner** (or User Access Administrator) to assign roles. If **Add role assignment** is greyed out, ask whoever manages that subscription.

---

### After the restore
- **Linked services**: Azure never exports passwords or keys, so open each one in the new factory (ADF Studio → **Manage → Linked services**), re-enter the key or connection string, then **Test connection → Save**.
- **Triggers** are created **stopped**. Start them when you're ready; the app gives you the commands.
- If your files moved to a **new storage account**, linked services that use the old address must point to the new one. Use **New account → Advanced → Replace storage URLs**, for example `oldaccount.dfs.core.windows.net=>newaccount.dfs.core.windows.net`.

---

### Common errors

| Message contains | Meaning | Fix |
|---|---|---|
| `AuthorizationFailed` / `does not have authorization` | The app has no role there | Step 4: Contributor on the new subscription, Reader on the old resource group |
| `AADSTS7000215` / `Invalid client secret` | The wrong secret was pasted | Use the secret's **Value**, not its Secret ID. Create a new secret if you lost it |
| `AADSTS700016` / `application ... was not found` | The Client ID belongs to a different tenant | App registrations live in one tenant. Check the Tenant ID, or create an app in this tenant |
| `'az' is not recognized` / `Azure CLI not found` | No Client ID was given and the Azure CLI isn't installed | Fill in Client ID and secret (step 3) |
| `AuthenticationFailed` (storage) | The storage key changed | Copy the connection string again (step 1) |
| `ContainerBeingDeleted` | A deleted container with the same name is still kept by soft delete | Wait for it to expire, or restore into a new storage account |
| Containers missing after backup | They're in another storage account | Back up that account with its own connection string |
