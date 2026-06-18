from __future__ import annotations
import io
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="IBSRS - Bank Reconciliation",
                   page_icon="🏦", layout="wide")

STATUS_BADGE = {
    "CLOSED_CLEAN": ("✅", "Account reconciles clean - confirm and close."),
    "CLOSED_WITH_ADJUSTMENTS": ("🟡", "Reconciled after suggested adjustments."),
    "ESCALATED": ("🟠", "Controller approval required before close."),
    "OPEN_EXCEPTIONS": ("🔴", "Unresolved exceptions - period cannot close."),
}


# ---------------------------------------------------------------- helpers
def api(path: str) -> str:
    return st.session_state.get("api_url", "http://127.0.0.1:8000").rstrip("/") + path


def get_json(path: str):
    r = requests.get(api(path), timeout=30)
    r.raise_for_status()
    return r.json()


def get_text(path: str) -> str:
    r = requests.get(api(path), timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- sidebar
st.sidebar.title("🏦 IBSRS")
st.sidebar.caption("Intelligent Bank Reconciliation System - "
                   "deterministic 6-agent pipeline")
st.session_state["api_url"] = st.sidebar.text_input(
    "FastAPI backend URL", st.session_state.get("api_url", "http://127.0.0.1:8000"))

try:
    bundles = get_json("/bundles")
    backend_ok = True
except requests.RequestException as exc:
    backend_ok = False
    st.sidebar.error(f"Backend unreachable: {exc}")
    st.title("IBSRS - Intelligent Bank Reconciliation System")
    st.warning("Start the backend first:  `uvicorn app.api:app --port 8000`")
    st.stop()

names = [b["name"] for b in bundles]
sel = st.sidebar.selectbox("Recon Bundle", names, index=0 if names else None)
sel_bundle = next((b for b in bundles if b["name"] == sel), None)
if sel_bundle:
    st.sidebar.caption(f"**{sel_bundle['bundle_id']}** | account "
                       f"{sel_bundle['account']} | period {sel_bundle['period']}")
    st.sidebar.write(sel_bundle["description"])

run_clicked = st.sidebar.button("▶ Run Reconciliation", type="primary",
                                use_container_width=True, disabled=not sel)

# --- policy editor (visible policy-driven behavior, per demo expectations)
with st.sidebar.expander("⚙️ Policy Pack (live)"):
    try:
        pol = get_json("/policy")
        materiality = st.number_input("Materiality threshold",
                                      value=float(pol["thresholds"]["materiality"]),
                                      step=500.0)
        auto_max = st.number_input("Auto-journal cap",
                                   value=float(pol["thresholds"]["auto_journal_max"]),
                                   step=50.0)
        fuzzy = st.slider("Fuzzy description threshold", 0.0, 1.0,
                          float(pol["matching"]["fuzzy_description_threshold"]), 0.01)
        date_win = st.slider("Match date window (days)", 0, 10,
                             int(pol["matching"]["date_window_days"]))
        st.markdown("**🤖 AI matching**")
        sem_thr = st.slider("Semantic match threshold", 0.0, 1.0,
                            float(pol.get("ai", {}).get("semantic_match_threshold", 0.75)),
                            0.01, help="Min blended (semantic+lexical) score for an "
                            "AI-assisted match in Agent C.")
        if st.button("Save policy", use_container_width=True):
            requests.put(api("/policy"), json={
                "thresholds": {"materiality": materiality,
                               "auto_journal_max": auto_max},
                "matching": {"fuzzy_description_threshold": fuzzy,
                             "date_window_days": date_win},
                "ai": {"semantic_match_threshold": sem_thr}},
                timeout=30).raise_for_status()
            st.success("Policy updated - re-run to see decisions change.")
    except requests.RequestException as exc:
        st.error(f"Policy load failed: {exc}")

# --- AI status + run mode
ai_state = {}
try:
    ai_state = get_json("/ai/status")
except requests.RequestException:
    pass
_emb = ai_state.get("embeddings_available")
_llm_state = ai_state.get("llm_state", "")
_fp = ai_state.get("key_fingerprint", "")
if ai_state.get("llm_available") and _llm_state == "ready":
    badge = f"🟢 LLM ({ai_state.get('reasoning_model')}) + semantic — key validated on run"
elif _emb and ai_state.get("key_present"):
    badge = f"🟡 Semantic matching on; LLM key present ({_llm_state})"
elif _emb:
    badge = "🟡 Semantic matching only (no LLM key)"
else:
    badge = "⚪ Deterministic mode (AI stack not installed)"
st.sidebar.caption(f"**AI status:** {badge}"
                   + (f"  \n🔑 server key: `{_fp}`" if ai_state.get("key_present")
                      else "  \n🔑 no key loaded"))
ai_choice = st.sidebar.radio("AI mode for next run", ["Auto", "Force ON", "Force OFF"],
                             horizontal=True,
                             help="Auto = use AI if available; financial decisions stay "
                                  "deterministic regardless.")
_USE_AI = {"Auto": None, "Force ON": True, "Force OFF": False}[ai_choice]

# --- statement upload (input mode (a) single statement / (b) with GL)
with st.sidebar.expander("📤 Upload statement"):
    up = st.file_uploader("Bank statement (CSV / MT940 / PDF)",
                          type=["csv", "mt940", "txt", "pdf"])
    gl_up = st.file_uploader("GL export (CSV, optional - enables matching)",
                             type=["csv"])
    auto_bal = st.checkbox("Auto-detect opening/closing from statement", value=True,
                           help="MT940 :60F:/:62F:, PDF 'Opening/Closing Balance', "
                                "or the CSV running_balance column.")
    u_open = u_close = None
    if not auto_bal:
        c1, c2 = st.columns(2)
        u_open = c1.number_input("Opening balance", value=0.0, step=100.0)
        u_close = c2.number_input("Closing balance", value=0.0, step=100.0)
    u_acct = st.text_input("Account ID", "UPL-0001")
    cc1, cc2 = st.columns(2)
    u_cur = cc1.text_input("Currency", "USD")
    u_period = cc2.text_input("Period", "2026-05")
    if st.button("Upload & run", type="primary", use_container_width=True,
                 disabled=up is None):
        files = {"file": (up.name, up.getvalue())}
        if gl_up is not None:
            files["gl_file"] = (gl_up.name, gl_up.getvalue())
        form = {"account_id": u_acct, "period": u_period, "currency": u_cur,
                "run_now": "true"}
        if _USE_AI is not None:
            form["use_ai"] = str(_USE_AI)
        if not auto_bal:
            form["opening_balance"] = u_open
            form["closing_balance"] = u_close
        with st.spinner(f"Uploading and running {up.name} ..."):
            r = requests.post(api("/upload-statement"), files=files, data=form,
                              timeout=300)
        if r.status_code != 200:
            st.error(f"Upload failed: {r.json().get('detail', r.text)}")
        else:
            data = r.json()
            st.session_state["last_run"] = data["run"]["run_id"]
            bal = (f"  ·  detected opening {data['opening_balance']:,.2f} / "
                   f"closing {data['closing_balance']:,.2f}"
                   if data.get("detected_opening") is not None else "")
            st.success(f"Uploaded `{data['bundle_name']}` and ran it "
                       f"→ status **{data['run']['status']}**{bal}"
                       + ("" if data["has_gl"] else "  (statement-only: no GL, so "
                          "matching is skipped — extraction/duplicates/risk shown)"))
            st.rerun()

st.sidebar.divider()
runs = get_json("/runs")
run_ids = [r["run_id"] for r in reversed(runs)]
hist = st.sidebar.selectbox("📁 Run history", ["(latest run)"] + run_ids)

# ---------------------------------------------------------------- main
st.title("Intelligent Bank Reconciliation System")
st.caption("Hybrid multi-agent pipeline: A intake → B extraction (🤖 LLM PDF) → "
           "C&D GL matching (🤖 semantic) & variance → E duplicates → "
           "H triage & 🤖 AI reasoning. Financial decisions stay deterministic.")

if run_clicked and sel:
    with st.spinner(f"Running hybrid 6-agent pipeline on {sel} "
                    "(AI semantic matching may take longer on first load) ..."):
        r = requests.post(api("/runs"),
                          json={"bundle_name": sel, "use_ai": _USE_AI}, timeout=300)
    if r.status_code != 200:
        st.error(f"Pipeline error: {r.json().get('detail', r.text)}")
        st.stop()
    st.session_state["last_run"] = r.json()["run_id"]

run_id = (st.session_state.get("last_run") if hist == "(latest run)" else hist)
if not run_id:
    st.info("Select a Recon Bundle on the left and click **Run Reconciliation**, "
            "or pick a previous run from the history.")
    st.stop()

try:
    run = get_json(f"/runs/{run_id}")
except requests.RequestException:
    st.error(f"Run `{run_id}` not found. Run a reconciliation first.")
    st.stop()

decision, metrics = run.get("decision", {}), run.get("metrics", {})
icon, blurb = STATUS_BADGE.get(decision.get("status", ""), ("ℹ️", ""))
st.subheader(f"{icon} {decision.get('status', '?')} - `{run_id}`")
st.write(f"{decision.get('summary', '')}  \n*{blurb}*")

m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
m1.metric("Bank match rate", f"{metrics.get('match_rate_bank', 0):.0%}")
m2.metric("GL match rate", f"{metrics.get('match_rate_gl', 0):.0%}")
m3.metric("🤖 AI semantic match", f"{metrics.get('semantic_match_rate', 0):.0%}",
          delta=f"{metrics.get('ai_assisted_matches', 0)} matches"
                if metrics.get('ai_assisted_matches') else None,
          help="Share of bank transactions matched thanks to AI semantic "
               "similarity (lexical matching alone would have missed them).")
m4.metric("Exceptions", metrics.get("exception_count", 0))
m5.metric("Journal entries", metrics.get("journal_entries", 0))
m6.metric("LLM calls", metrics.get("llm_calls", 0))
m7.metric("Deterministic hash", str(metrics.get("deterministic_hash", ""))[:8])

tabs = st.tabs(["📄 Recon Statement", "💳 Transactions", "🔗 Matching",
                "⏱ Timing Diffs", "👯 Duplicates", "⚠️ Exceptions",
                "📒 Journal Entries", "📜 Audit Log", "📊 Metrics"])

with tabs[0]:
    st.markdown(get_text(f"/runs/{run_id}/artifacts/recon_statement.md"))

with tabs[1]:
    tx = get_json(f"/runs/{run_id}/artifacts/transactions.json")
    st.caption(f"Opening {tx['opening_balance']:,.2f} -> computed closing "
               f"{tx['computed_closing_balance']:,.2f} "
               f"({'rolls forward ✓' if tx['balance_reconciles'] else 'MISMATCH ✗'})"
               + (" | synthetic fallback used" if tx.get("synthetic_fallback_used") else ""))
    df = pd.DataFrame([{**t, "evidence": t["evidence"]["locator"]}
                       for t in tx["transactions"]])
    st.dataframe(df, use_container_width=True, hide_index=True)

with tabs[2]:
    mr = get_json(f"/runs/{run_id}/artifacts/match_result.json")
    ai_matches = mr.get("ai_assisted_matches", 0)
    st.markdown(f"**Matched pairs: {len(mr['matched'])}**"
                + (f"  ·  🤖 **{ai_matches}** AI-assisted (semantic) "
                   f"({mr.get('semantic_match_rate', 0):.0%} of bank txns)"
                   if ai_matches else ""))
    if mr["matched"]:
        mdf = pd.DataFrame([{
            "AI": "🤖" if m.get("ai_assisted") else "",
            "match_id": m["match_id"], "type": m["match_type"],
            "bank": ", ".join(m["bank_txn_ids"]), "gl": ", ".join(m["gl_ids"]),
            "score": m["score"],
            "semantic": "-" if m.get("semantic_score") is None
                        else round(m["semantic_score"], 3),
            "exact": "-" if m.get("exact_score") is None
                     else round(m["exact_score"], 3),
            "rationale": m["rationale"]}
            for m in mr["matched"]])

        def _hl(row):
            return ['background-color: #1b3a2b' if row["AI"] else '' for _ in row]
        st.dataframe(mdf.style.apply(_hl, axis=1), use_container_width=True,
                     hide_index=True)
        if ai_matches:
            st.caption("🤖 Highlighted rows: semantic similarity was the deciding "
                       "factor (lexical/token score alone was below threshold). "
                       "Amount and date remained exact deterministic guards.")
    c1, c2 = st.columns(2)
    for col, key, label in ((c1, "unmatched_bank", "Unmatched bank items"),
                            (c2, "unmatched_gl", "Unmatched GL items")):
        col.markdown(f"**{label}: {len(mr[key])}**")
        if mr[key]:
            col.dataframe(pd.DataFrame(mr[key]), use_container_width=True,
                          hide_index=True)

with tabs[3]:
    csv_text = get_text(f"/runs/{run_id}/artifacts/timing_diffs.csv")
    tdf = pd.read_csv(io.StringIO(csv_text))
    if tdf.empty:
        st.success("No timing differences.")
    else:
        st.dataframe(tdf, use_container_width=True, hide_index=True)
        st.bar_chart(tdf.groupby("category")["amount"].sum())

with tabs[4]:
    dup = get_json(f"/runs/{run_id}/artifacts/duplicates.json")
    if not dup["groups"]:
        st.success("No duplicates detected.")
    for g in dup["groups"]:
        st.warning(f"**{g['dup_id']} [{g['kind']}]** ({g['confidence']:.0%}) - "
                   f"{g['detail']}\n\n*Suggested action:* {g['suggested_action']} "
                   f"| items: {', '.join(g['txn_ids'])}")

with tabs[5]:
    st.markdown(get_text(f"/runs/{run_id}/artifacts/exceptions.md"))

with tabs[6]:
    je = get_json(f"/runs/{run_id}/artifacts/journal_entries.json")
    if not je["entries"]:
        st.success("No journal entries suggested.")
    for e in je["entries"]:
        with st.expander(f"{e['je_id']} - {e['memo']} [{e['status']}]"):
            st.dataframe(pd.DataFrame(e["lines"]), use_container_width=True,
                         hide_index=True)
            st.caption("ERP-ready payload")
            st.json(e["erp_payload"])

with tabs[7]:
    ai_ins = run.get("ai_insights", {})
    if ai_ins:
        gen = ai_ins.get("generated_by", "")
        is_llm = gen not in ("", "deterministic-template")
        lstate = ai_ins.get("llm_state", "")
        header = (f"### 🤖 AI Reasoning  \n*Generated by: `{gen}` (GPT)*"
                  if is_llm else
                  f"### 🤖 AI Reasoning *(deterministic template — LLM unavailable: "
                  f"{lstate or 'offline'})*")
        box = st.success if is_llm else st.info
        body = ai_ins.get("ai_reasoning", "")
        risks = ai_ins.get("key_risks", [])
        focus = ai_ins.get("recommended_focus", "")
        box(f"{header}\n\n{body}"
            + ("\n\n**Key risks:**\n" + "\n".join(f"- {r}" for r in risks) if risks else "")
            + (f"\n\n**Recommended focus:** {focus}" if focus else ""))
        st.caption("Reasoning explains the result only — the status, routing and "
                   "journal entries are produced by deterministic rules (SOX).")
        st.divider()
    st.markdown(get_text(f"/runs/{run_id}/artifacts/audit_log.md"))

with tabs[8]:
    st.json(metrics)
    with st.expander("🤖 LLM / AI call log (llm_calls.log)"):
        try:
            st.code(get_text(f"/runs/{run_id}/artifacts/llm_calls.log"))
        except requests.RequestException:
            st.caption("No LLM call log for this run.")
