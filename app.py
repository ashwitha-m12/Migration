"""
Cloud Account Migration - desktop app.
Start it with Start.bat (Windows) or:  python -m streamlit run app.py
"""
import os
import platform as _os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from core import engine, profiles
from core.registry import available, load

load_dotenv()                         # optional: values from .env are used as starting values
st.set_page_config(page_title="Cloud Account Migration", page_icon=":material/move_up:", layout="wide")

SIDES = {"SRC": "Old account (expiring)", "TGT": "New account"}
SELECTABLE = {"database": "DATABASES", "container": "CONTAINERS"}   # kinds the user can tick at backup time
STATUS_COLORS = {"OK": "#d6f5dd", "FAILED": "#ffd9d9", "MANUAL": "#fff1c2", "SKIPPED": "#e8e8e8"}
OUT = "Migration_Backups"


# ----------------------------------------------------------------------------- helpers
def conn_cls(platform):
    return load(platform)


def init_values(platform):
    """Starting values: what's in session, else .env / environment, else the field default."""
    key = f"vals_{platform}"
    if key not in st.session_state:
        cls = conn_cls(platform)
        st.session_state[key] = {
            side: {f.key: os.getenv(f"{side}_{cls.env_prefix}_{f.key}", f.default) or ""
                   for f in cls.fields if side in f.sides}
            for side in SIDES}
    return st.session_state[key]


def apply_env(platform, extra=None):
    """Hand the form values to the engine (it reads SRC_*/TGT_* settings)."""
    cls = conn_cls(platform)
    for side, kv in st.session_state[f"vals_{platform}"].items():
        for k, v in kv.items():
            var = f"{side}_{cls.env_prefix}_{k}"
            if v:
                os.environ[var] = str(v)
            else:
                os.environ.pop(var, None)
    for var, v in (extra or {}).items():
        if v:
            os.environ[var] = str(v)
        else:
            os.environ.pop(var, None)


def results_table(results):
    df = pd.DataFrame([asdict(r) for r in results])
    if df.empty:
        st.info("Nothing to show.")
        return
    df = df.rename(columns={"status": "Status", "kind": "Type", "name": "Name", "message": "Details"})
    st.dataframe(df.style.map(lambda v: f"background-color:{STATUS_COLORS.get(v, '')}", subset=["Status"]),
                 use_container_width=True, hide_index=True)


def summary(counts):
    cols = st.columns(4)
    for col, (k, label) in zip(cols, (("OK", "Done"), ("FAILED", "Failed"), ("MANUAL", "Needs you"), ("SKIPPED", "Skipped"))):
        col.metric(label, counts.get(k, 0))


def run_with_log(title, fn):
    """Run a long job, streaming its progress messages into an expandable status box."""
    with st.status(title, expanded=True) as box:
        try:
            out = fn(lambda m: box.write(m))
            box.update(label=f"{title} - finished", state="complete", expanded=False)
            return out
        except Exception as e:
            box.update(label=f"{title} - stopped", state="error", expanded=True)
            st.error((str(e).strip().splitlines() or [type(e).__name__])[0])
            return None


def package_picker(platform, key):
    pkgs = engine.list_packages(OUT, platform)
    if not pkgs:
        st.info("No backups yet. Make one on the **Back up** page first.")
        return None
    labels = {str(p["path"]): f"{p['path'].name}   ·   {p.get('resource_count', '?')} resources   ·   {p.get('created_at', '')}"
              for p in pkgs}
    choice = st.selectbox("Backup to use", list(labels), format_func=labels.get, key=key)
    return Path(choice)


def open_folder(path: Path):
    try:
        if _os.system() == "Windows":
            os.startfile(path.resolve())                       # noqa: S606
        elif _os.system() == "Darwin":
            subprocess.Popen(["open", str(path.resolve())])
        else:
            subprocess.Popen(["xdg-open", str(path.resolve())])
    except Exception as e:
        st.warning(f"Couldn't open the folder: {e}")


# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("Cloud Account Migration")
    st.caption("Copy everything from an expiring account into a new one.")
    platform = st.selectbox("Platform", available(), format_func=lambda p: getattr(conn_cls(p), "title", p))
    cls = conn_cls(platform)
    values = init_values(platform)
    page = st.radio("Steps", ["1. Accounts", "2. Back up", "3. Restore", "4. Check", "Backups & reports"],
                    label_visibility="collapsed")
    st.divider()
    tested = st.session_state.get(f"tested_{platform}", {})
    for side, label in SIDES.items():
        ok = tested.get(side)
        st.markdown(f"{':green[●]' if ok else ':gray[○]'} {label}{' - connected' if ok else ''}")


# ----------------------------------------------------------------------------- 1. accounts
if page == "1. Accounts":
    st.header("1. Accounts")
    st.write("Enter the login details for the old and the new account. You can save them as a profile for next time.")

    if "profile_msg" in st.session_state:
        st.success(st.session_state.pop("profile_msg"))
    with st.expander("Saved profiles", expanded=bool(profiles.list_profiles(platform))):
        c1, c2 = st.columns(2)
        with c1:
            names = profiles.list_profiles(platform)
            pick = st.selectbox("Load a profile", ["-"] + names)
            b1, b2 = st.columns(2)
            if b1.button("Load", disabled=pick == "-"):
                loaded = profiles.load(pick, platform, cls.fields)
                for side in SIDES:
                    values[side].update(loaded.get(side, {}))
                    for fkey, fval in loaded.get(side, {}).items():
                        st.session_state[f"{platform}_{side}_{fkey}"] = fval   # put the values into the form boxes
                st.session_state.pop(f"tested_{platform}", None)
                for side in SIDES:
                    st.session_state.pop(f"testmsg_{platform}_{side}", None)
                st.session_state["profile_msg"] = f"Loaded profile '{pick}'. Click Test connection for each account."
                st.rerun()
            if b2.button("Delete", disabled=pick == "-"):
                profiles.delete(pick, platform, cls.fields)
                st.rerun()
        with c2:
            new_name = st.text_input("Save current details as", placeholder="e.g. snowflake-nov-2026")
            if st.button("Save profile", disabled=not new_name):
                st.success(profiles.save(new_name, platform, cls.fields, values))
            if not profiles.secrets_supported():
                st.caption("No credential store found on this computer, so passwords are not saved in profiles.")

    cols = st.columns(2)
    for col, (side, label) in zip(cols, SIDES.items()):
        with col, st.container(border=True):
            st.subheader(label)
            normal = [f for f in cls.fields if side in f.sides and not f.advanced]
            adv = [f for f in cls.fields if side in f.sides and f.advanced]

            def render(f, side=side):
                k = f"{platform}_{side}_{f.key}"
                if k not in st.session_state:              # first time: start from saved / .env / default value
                    st.session_state[k] = values[side].get(f.key, "") or ""
                label_txt = f.label + (" *" if f.required else "")
                if f.options:
                    opts = list(f.options)
                    if st.session_state[k] not in opts:
                        st.session_state[k] = opts[0]
                    v = st.selectbox(label_txt, opts, key=k, help=f.help or None,
                                     format_func=lambda o: o or "Password (default)")
                else:
                    v = st.text_input(label_txt, key=k, help=f.help or None,
                                      type="password" if f.secret else "default")
                values[side][f.key] = v

            for f in normal:
                render(f)
            if adv:
                with st.expander("Advanced"):
                    for f in adv:
                        render(f)
            if st.button("Test connection", key=f"test_{side}", type="primary"):
                missing = [f.label for f in cls.fields if f.required and side in f.sides and not values[side].get(f.key)]
                if missing:
                    st.error("Please fill in: " + ", ".join(missing))
                else:
                    apply_env(platform)
                    with st.spinner("Connecting ..."):
                        ok, msg = engine.test_connection(platform, side)
                    st.session_state.setdefault(f"tested_{platform}", {})[side] = ok
                    st.session_state[f"testmsg_{platform}_{side}"] = (ok, msg)
                    st.rerun()                      # redraw so the sidebar shows the new status at once
            if f"testmsg_{platform}_{side}" in st.session_state:
                ok, msg = st.session_state[f"testmsg_{platform}_{side}"]
                (st.success if ok else st.error)(msg)


# ----------------------------------------------------------------------------- 2. backup
elif page == "2. Back up":
    st.header("2. Back up the old account")
    st.write("The backup is saved on this computer, so it survives even after the old account is closed.")
    apply_env(platform)

    if st.button("Scan old account"):
        with st.spinner("Scanning ..."):
            try:
                st.session_state[f"inv_{platform}"] = engine.discover(platform, "SRC")
            except Exception as e:
                st.error((str(e).strip().splitlines() or [type(e).__name__])[0])

    inv = st.session_state.get(f"inv_{platform}")
    extra = {}
    if inv:
        counts = pd.Series([r.kind for r in inv]).value_counts().rename_axis("Type").reset_index(name="Found")
        c1, c2 = st.columns([1, 2])
        c1.dataframe(counts, hide_index=True, use_container_width=True)
        with c2:
            for kind, setting in SELECTABLE.items():
                names = [r.name for r in inv if r.kind == kind]
                if names:
                    chosen = st.multiselect(f"{kind.title()}s to include", names, default=names)
                    extra[f"SRC_{cls.env_prefix}_{setting}"] = "" if set(chosen) == set(names) else ",".join(chosen)
            if platform == "snowflake":
                data = st.checkbox("Include table data", value=True)
                extra["SRC_SF_SKIP_DATA"] = "" if data else "true"
            with st.expander("Everything found"):
                st.dataframe(pd.DataFrame([{"Type": r.kind, "Name": r.name} for r in inv]),
                             hide_index=True, use_container_width=True)
    else:
        st.caption("Tip: scan first to choose what to include. Or just press Back up to take everything.")

    if st.button("Back up now", type="primary"):
        apply_env(platform, extra)
        out = run_with_log("Backing up", lambda log: engine.backup(platform, OUT, log=log))
        if out:
            pkg, results, counts = out
            st.success(f"Backup saved: {pkg.name}")
            summary(counts)
            results_table(results)


# ----------------------------------------------------------------------------- 3. restore
elif page == "3. Restore":
    st.header("3. Restore into the new account")
    apply_env(platform)
    pkg = package_picker(platform, "restore_pkg")
    if pkg:
        mode = "everything"
        if platform == "snowflake":
            mode = st.radio("What to restore", ["everything", "worksheets only"], horizontal=True,
                            help="'worksheets only' copies just your Workspaces files and doesn't touch databases.")
        target = values["TGT"].get("ACCOUNT") or values["TGT"].get("HOST") or "the new account"
        st.warning(f"This rebuilds the backed-up objects in **{target}**. Use a new or empty account - "
                   "existing objects with the same names are replaced.")
        sure = st.checkbox("I understand, restore into the new account")
        if st.button("Restore now", type="primary", disabled=not sure):
            only = ["workspaces"] if mode == "worksheets only" else None
            out = run_with_log("Restoring", lambda log: engine.restore(pkg, log=log, only=only))
            if out:
                _, results, counts = out
                st.success("Restore finished. Next: open **4. Check**.")
                summary(counts)
                manual = [r for r in results if r.status == "MANUAL"]
                if manual:
                    st.subheader("Things you still need to do")
                    for r in manual:
                        st.markdown(f"- **{r.name}** - {r.message}")
                results_table(results)


# ----------------------------------------------------------------------------- 4. check
elif page == "4. Check":
    st.header("4. Check the new account against the backup")
    st.write("Compares every object, row count and a fingerprint of every table's data.")
    apply_env(platform)
    pkg = package_picker(platform, "check_pkg")
    if pkg and st.button("Run check", type="primary"):
        out = run_with_log("Checking", lambda log: engine.validate(pkg, log=log))
        if out:
            _, results, counts = out
            if counts.get("FAILED", 0) == 0:
                st.success("Everything matches.")
            else:
                st.error(f"{counts['FAILED']} difference(s) found - see the red rows.")
            summary(counts)
            results_table(results)


# ----------------------------------------------------------------------------- reports
else:
    st.header("Backups & reports")
    pkg = package_picker(platform, "report_pkg")
    if pkg:
        if st.button("Open backup folder"):
            open_folder(pkg)
        tabs = [t for t in ("backup", "restore", "validate") if (pkg / f"report_{t}.md").exists()]
        if tabs:
            for tab, name in zip(st.tabs([t.title() for t in tabs]), tabs):
                with tab:
                    st.markdown((pkg / f"report_{name}.md").read_text(encoding="utf-8"))
