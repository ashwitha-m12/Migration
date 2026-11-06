### Before you start
- You need to be able to sign in to **both** accounts: the old (expiring) one and the new one.
- Use a role that can see and create everything. **ACCOUNTADMIN** is simplest.
- If your data pipeline loads from cloud storage (stages), finish the **storage** part first, for example with the Azure platform in this app.

---

### Where to find each value

**Account identifier**
In Snowsight (the Snowflake website), open a worksheet and run:
```sql
SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME();
```
Copy the result, which looks like `ABCDEFG-XY12345`. Or click your name (bottom-left) → **Account** → **View account details** → **Account identifier**.
Do **not** use the short locator (like `CU19564`) on its own.

**Username**
Your Snowflake login name. In a worksheet: `SELECT CURRENT_USER();`

**Password and login type**
- Normal password: leave **Login type** on *Password (default)*.
- If signing in asks for a **Duo** approval on your phone: choose `username_password_mfa`, and approve the push when you click Test connection.
- If you sign in with **Google or Microsoft**: choose `externalbrowser`. A browser window opens to sign in.

**Role**
`ACCOUNTADMIN` is recommended. With a smaller role, objects the role can't see won't be copied.

**Warehouse**
Any warehouse the role can use. A new account always has `COMPUTE_WH`. To list them: `SHOW WAREHOUSES;`

---

### New account → Advanced (only if your storage moved)
- **Azure tenant ID**: only if the new storage integrations must point to a **different** Azure account. Find it in Azure Portal → Microsoft Entra ID → Overview.
- **Replace storage URLs**: only if your files moved to a new storage account, for example `oldaccount.blob.core.windows.net=>newaccount.blob.core.windows.net`.

Leave both empty if you still use the same storage.

---

### Common errors

| Message contains | Fix |
|---|---|
| `Incorrect username or password` | Check the password. If unsure, an ACCOUNTADMIN can reset it: `ALTER USER <name> SET PASSWORD = '...';` |
| `404` / `could not connect` / `account ... not found` | The account identifier is in the wrong format. Use the query above |
| `MFA` / `multi-factor` | Set Login type to `username_password_mfa` |
| `warehouse ... does not exist` | Use a warehouse from `SHOW WAREHOUSES;` |
| `insufficient privileges` | Use the ACCOUNTADMIN role |
