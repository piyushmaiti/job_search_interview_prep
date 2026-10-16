"""The screens that are about you rather than about searching: the profile, a new role, and a role's details."""
from __future__ import annotations

import streamlit as st

import candidate
import safetext

S = st.session_state
PROFILE, NEW_ROLE = "__profile__", "__new__"
CV_TYPES = ["pdf", "docx", "md", "txt"]


def css() -> str:
    """Styles for the screens in this file, using the colours the app already defines (var(--...))."""
    return """
.st-key-viewbar { margin: .2rem 0 1.4rem; }
.st-key-viewbar [data-testid="stButtonGroup"] { gap: .35rem; flex-wrap: wrap; }
.st-key-viewbar [data-testid="stButtonGroup"] button { border-radius: 999px !important; padding: .25rem 1rem !important; min-height: 0 !important;
  background: transparent !important; border: 1px solid var(--line) !important; color: var(--muted) !important; }
.st-key-viewbar [data-testid="stButtonGroup"] button * { color: var(--muted) !important; }
.st-key-viewbar [data-testid="stButtonGroup"] button:hover { border-color: var(--accent) !important; }
.st-key-viewbar [data-testid="stButtonGroup"] button[aria-checked="true"], .st-key-viewbar [data-testid="stButtonGroup"] button[kind$="Active"],
.st-key-viewbar [data-testid="stButtonGroup"] button[data-testid$="Active"] { background: var(--accent) !important; border-color: var(--accent) !important; }
.st-key-viewbar [data-testid="stButtonGroup"] button[aria-checked="true"] *, .st-key-viewbar [data-testid="stButtonGroup"] button[data-testid$="Active"] * { color: var(--accent-fg) !important; }
[class*="st-key-page"] { max-width: 920px; }
.st-key-modebar { margin: .2rem 0 1.2rem; border-bottom: 1px solid var(--line); }
.st-key-modebar [data-testid="stButtonGroup"] { gap: 1.6rem; flex-wrap: wrap; }
.st-key-modebar [data-testid="stButtonGroup"] button { margin-right: 1.5rem !important; }
.st-key-modebar [data-testid="stButtonGroup"] button { background: transparent !important; border: 0 !important; border-bottom: 2px solid transparent !important;
  border-radius: 0 !important; padding: .35rem 0 .5rem !important; min-height: 0 !important; color: var(--muted) !important; }
.st-key-modebar [data-testid="stButtonGroup"] button * { color: var(--muted) !important; font-size: 15px !important; }
.st-key-modebar [data-testid="stButtonGroup"] button:hover * { color: var(--fg) !important; }
.st-key-modebar [data-testid="stButtonGroup"] button[aria-checked="true"], .st-key-modebar [data-testid="stButtonGroup"] button[data-testid$="Active"] { border-bottom-color: var(--accent) !important; }
.st-key-modebar [data-testid="stButtonGroup"] button[aria-checked="true"] *, .st-key-modebar [data-testid="stButtonGroup"] button[data-testid$="Active"] * { color: var(--fg) !important; font-weight: 600 !important; }
.st-key-page_prep [data-testid="stMarkdownContainer"] :is(h1, h2, h3), .st-key-page_mock [data-testid="stMarkdownContainer"] :is(h1, h2) { font-family: "Literata", Georgia, serif !important;
  font-weight: 500 !important; font-size: 19px !important; letter-spacing: 0 !important; color: var(--fg) !important; padding: 1.1rem 0 .2rem !important; }
.st-key-page_prep [data-testid="stMarkdownContainer"] p, .st-key-page_prep li, .st-key-page_mock blockquote p { font-size: 16px !important; line-height: 1.65 !important; }
.st-key-page_prep [data-testid="stCaptionContainer"], .st-key-page_mock [data-testid="stCaptionContainer"] { color: var(--muted) !important; opacity: 1 !important; max-width: 62ch; }
.st-key-page_mock blockquote { border-left: 3px solid var(--accent) !important; background: transparent !important; padding: .2rem 0 .2rem 1rem !important; margin: .4rem 0 1rem !important; }
.st-key-page_mock blockquote p { font-family: "Literata", Georgia, serif !important; font-size: 20px !important; line-height: 1.5 !important; color: var(--fg) !important; }
[class*="st-key-page_"] [data-testid="stAlert"] { background: var(--panel) !important; border: 1px solid var(--line) !important; border-left: 3px solid var(--accent) !important;
  border-radius: 6px !important; color: var(--fg) !important; }
[class*="st-key-page_"] [data-testid="stAlert"] * { color: var(--fg) !important; background: transparent !important; }
.st-key-page_prep [data-testid="stSelectbox"] { max-width: 420px; }
[class*="st-key-page"] h2, .st-key-rolehead h2 { font-family: "Literata", Georgia, serif !important; font-weight: 400 !important; font-size: 28px !important;
  letter-spacing: 0 !important; color: var(--fg) !important; padding: 0 0 .15rem !important; }
[class*="st-key-page"] h3 { font-size: 15px !important; letter-spacing: 0 !important; color: var(--fg) !important; padding: 1.2rem 0 .3rem !important; }
[data-testid="stTextArea"] textarea, [data-baseweb="textarea"], [data-baseweb="textarea"] > div, [data-testid="stTextAreaRootElement"]
  { background: var(--panel) !important; color: var(--fg) !important; -webkit-text-fill-color: var(--fg) !important; border-color: var(--line) !important; }
[data-testid="stTextArea"] textarea::placeholder, [data-testid="stTextInput"] input::placeholder { color: var(--muted) !important; -webkit-text-fill-color: var(--muted) !important; opacity: 1; }
""".strip()      # no blank lines: a blank line would end the <style> block that app.py puts this into


def _round(name: str) -> int:
    """Bumping a round gives a fresh, empty upload box (an uploader cannot be cleared any other way)."""
    return S.setdefault(f"round_{name}", 0)


def _next_round(name: str) -> None:
    S[f"round_{name}"] = _round(name) + 1


def flash() -> None:
    msg = S.pop("flash", None)
    if msg:
        (st.success if msg[0] else st.error)(msg[1])


def view_bar() -> str:
    """The top-level choice: Profile, one of the roles, or a new role. Returns the chosen value."""
    roles = candidate.list_roles()
    labels = {PROFILE: "Profile", NEW_ROLE: "+ New role"} | {r.slug: r.name for r in roles}
    options = [PROFILE] + [r.slug for r in roles] + [NEW_ROLE]
    if S.get("goto") in options:            # another screen asked to open a view; this must happen before the widget exists
        S.view = S.pop("goto")
    S.pop("goto", None)
    if S.get("view") not in options:
        S.view = roles[0].slug if roles and candidate.has_profile_cv() else PROFILE
    with st.container(key="viewbar"):
        picked = st.segmented_control("View", options, key="view", format_func=labels.get, label_visibility="collapsed")
    if picked is None:                      # clicking the chosen one again would deselect it
        S.goto = options[0]
        st.rerun()
    return picked


# ---------------------------------------------------------------- reading what was uploaded
# ---------------------------------------------------------------- profile
def profile_page(rebuild_index, index) -> None:
    with st.container(key="page_profile"):
        _profile_page(rebuild_index, index)


def _legacy_question(rebuild_index) -> None:
    """Files that earlier versions wrote into the profile folder are hidden and unused. The tool asks whether it may delete them."""
    files = candidate.legacy_to_ask_about()
    if not files:
        return
    st.markdown("### Leftover files")
    st.warning("Files left by earlier versions of this tool were found in your workspace: " + ", ".join(p.name for p in files) +
               ". The tool no longer uses them and hides them from your sources. Can the tool delete them for you? "
               "Only these files are deleted. Your CV, About you and papers are not touched.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, delete them", key="legacy-del", use_container_width=True):
        n = candidate.delete_legacy_files()
        rebuild_index()
        S.flash = (True, f"Deleted {n} file{'s' if n != 1 else ''}.")
        st.rerun()
    if c2.button("No, keep them", key="legacy-keep", use_container_width=True):
        candidate.keep_legacy_files()
        S.flash = (True, "Kept. They stay hidden and are not asked about again.")
        st.rerun()


def _index_status(index) -> None:
    """What is really in the search index for the CV and About you, counted from the index itself."""
    st.markdown("### 4. What is indexed")
    st.caption("Counted from the real search index, not assumed. Your whole CV and About you also go to the prep tools in full.")
    rows = candidate.index_report(index)
    if not rows:
        st.caption("Nothing to check yet. Save your CV above.")
        return
    for r in rows:
        if r["pieces"]:
            st.markdown(f"**{r['name']}** ({r['file']}): {r['chars']:,} characters in {r['pieces']} index piece{'s' if r['pieces'] != 1 else ''}, "
                        f"{r['covered']:.0%} of the text found there.")
        else:
            st.markdown(f"**{r['name']}** ({r['file']}): {r['chars']:,} characters, not in the index.")
        for note in r["notes"]:
            st.warning(f"{r['name']} {note}")


def _profile_page(rebuild_index, index) -> None:
    """Upload once; everything is used exactly as given and shared by every role."""
    st.markdown("## Your profile")
    st.caption("One profile for every role. Add your CV, say something about yourself, and add your papers if you have any. "
               "Everything is used exactly as you give it: nothing is rewritten, summarised or tidied. Your files stay in the workspace folder "
               "on this computer. They are sent to Claude only inside your questions and the prep tools.")
    flash()

    st.markdown("### 1. Resume or CV")
    cv = candidate.cv_path()
    if cv:
        st.markdown(f"Current file: **{cv.name}**")
    f = st.file_uploader("CV or resume (PDF, Word, Markdown or text)", type=CV_TYPES, key=f"cv_up_{_round('cv')}",
                         label_visibility="visible" if not cv else "collapsed")
    if st.button("Replace CV" if cv else "Save CV", key="cv-save", disabled=f is None):
        try:
            candidate.save_cv(f.name, f.getvalue())
            rebuild_index()
            _next_round("cv")
            S.flash = (True, "CV saved and indexed. Your whole CV is used as it is.")
        except Exception as e:
            S.flash = (False, f"Could not save the CV: {e}")
        st.rerun()

    st.markdown("### 2. About you (optional)")
    st.caption("It makes your answers sound like you: your story, why this field, what you want next. "
               "A statement of purpose works well here. It is used exactly as you write it.")
    about = st.text_area("About you", value=candidate.read_about(), height=180, key=f"about_{_round('about')}", label_visibility="collapsed",
                         placeholder="Paste or write it here.")
    up = st.file_uploader("or upload a file", type=["pdf", "docx", "md", "txt"], key=f"about_up_{_round('aboutf')}")
    if st.button("Save about you", key="about-save", disabled=(about.strip() == candidate.read_about() and up is None)):
        try:
            text = about
            if up is not None:
                data = up.getvalue()
                if len(data) > candidate.MAX_UPLOAD_BYTES:
                    raise ValueError("this file is larger than 20 MB")
                if up.name.lower().endswith(".docx"):
                    text = candidate.docx_text(data)
                elif up.name.lower().endswith(".pdf"):
                    import io
                    from pypdf import PdfReader
                    text = "\n\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:200])
                else:
                    text = data.decode("utf-8", errors="replace")
            candidate.save_about(text)
            rebuild_index()
            _next_round("about")
            _next_round("aboutf")
            S.flash = (True, "Saved and indexed. Your text is used exactly as you wrote it.")
        except Exception as e:
            S.flash = (False, f"Could not save: {e}")
        st.rerun()

    st.markdown("### 3. Publications (optional)")
    st.caption("Each paper you add becomes a source you can ask about.")
    for name in candidate.list_publications():
        c1, c2 = st.columns([8, 1], vertical_alignment="center")
        c1.markdown(name)
        if c2.button("", key=f"pub-del-{name}", icon=":material/delete:", type="tertiary", help="Remove this publication"):
            candidate.delete_publication(name)
            rebuild_index()
            st.rerun()
    pubs = st.file_uploader("Add publications", type=["pdf", "md", "txt"], accept_multiple_files=True, key=f"pub_up_{_round('pub')}",
                            label_visibility="collapsed")
    if st.button("Add publications", key="pub-save", disabled=not pubs):
        bad = []
        for p in pubs:
            try:
                candidate.save_publication(p.name, p.getvalue())
            except Exception as e:
                bad.append(f"{p.name}: {e}")
        rebuild_index()
        _next_round("pub")
        S.flash = (not bad, "Added." if not bad else "Some files were not added: " + "; ".join(bad))
        st.rerun()

    _index_status(index)
    _legacy_question(rebuild_index)
    st.caption("Next: add a role with + New role at the top.")


# ---------------------------------------------------------------- roles
def new_role_page(rebuild_index) -> None:
    with st.container(key="page_newrole"):
        _new_role_page(rebuild_index)


def _new_role_page(rebuild_index) -> None:
    st.markdown("## New role")
    st.caption("One role is one job you are preparing for. It gets its own tab with its own job description, CV, chats and sources. "
               "Your profile is shared.")
    if not candidate.has_profile_cv():
        st.info("Add your CV on the Profile page first. The tool needs it to prepare you.")
        return
    flash()
    r = _round("newrole")
    k = lambda n: f"nr_{n}_{r}"
    c1, c2 = st.columns(2)
    name = c1.text_input("Job title", key=k("name"), placeholder="Imaging Scientist")
    company = c2.text_input("Company or lab (optional)", key=k("company"))
    kind = st.radio("Type of role", candidate.KINDS, horizontal=True, key=k("kind"),
                    help="Industry roles and academic roles are asked about differently.")
    jd = st.text_area("Job description", height=240, key=k("jd"), placeholder="Paste the posting here. It is saved as job_description.md.")
    intent = st.text_area("Your intent for this application", height=110, key=k("intent"),
                          placeholder="What do you want this application to show? For example: that my imaging work transfers to drug development.")
    st.markdown("**CV or resume for this role**")
    st.caption("Upload the version you are sending to this employer.")
    f = st.file_uploader("CV for this role", type=CV_TYPES, key=k("cv"), label_visibility="collapsed")
    same = st.checkbox("Use my main CV instead", key=k("same"), help="Copies the CV from your profile into this role.")
    if st.button("Create role", key="role-create", type="primary"):
        try:
            role = candidate.create_role(name, company, kind, jd, intent, f.name if f else "", f.getvalue() if f else None, use_main_cv=same and f is None)
        except ValueError as e:
            st.error(str(e))
            return
        with st.spinner("Indexing the new role..."):
            rebuild_index()
        _next_round("newrole")
        S.goto = role.slug
        S.flash = (True, f"Role created: {role.label}.")
        st.rerun()


def role_details(role: candidate.Role, rebuild_index) -> bool:
    """The header of a role tab: its title, and a drawer with the job description, intent and CV. Returns True if the role was deleted."""
    head = st.container(key="rolehead")
    head.markdown(f"## {role.name}")
    head.caption(" · ".join(x for x in (role.company, f"{role.kind} role") if x))
    flash()
    if S.get("confirm_role_delete") == role.slug:
        st.warning(f"Delete **{role.name}**, with its job description, CV and notes? Its saved chats stay. This cannot be undone.")
        y, n = st.columns(2)
        if y.button("Delete role", key="role-del-yes", use_container_width=True):
            candidate.delete_role(role.slug)
            S.confirm_role_delete = None
            S.goto = PROFILE
            with st.spinner("Re-indexing..."):
                rebuild_index()
            st.rerun()
        if n.button("Keep", key="role-del-no", use_container_width=True):
            S.confirm_role_delete = None
            st.rerun()
    with st.expander("Job description, intent and CV for this role"):
        jd = st.text_area("Job description", value=candidate.job_description_text(role), height=220, key=f"jd_{role.slug}")
        intent = st.text_area("Your intent", value=candidate.intent_text(role), height=100, key=f"intent_{role.slug}")
        cv = role.cv()
        st.markdown(f"CV for this role: **{cv.name if cv else 'none'}**")
        f = st.file_uploader("Replace the CV for this role", type=CV_TYPES, key=f"rolecv_{role.slug}_{_round('rolecv')}")
        save, gone = st.columns(2)
        if gone.button("Delete role", key="role-del", use_container_width=True, help="Delete this role, its job description, CV and notes"):
            S.confirm_role_delete = role.slug
            st.rerun()
        if save.button("Save changes", key="role-save", use_container_width=True):
            try:
                candidate.update_role_texts(role, jd, intent)
                if f is not None:
                    candidate.replace_role_cv(role, f.name, f.getvalue())
                    _next_round("rolecv")
                with st.spinner("Re-indexing..."):
                    rebuild_index()
                S.flash = (True, "Saved.")
            except Exception as e:
                S.flash = (False, f"Could not save: {e}")
            st.rerun()
    return False


# ---------------------------------------------------------------- what to do in a role
MODES = ("Research", "Prep tools", "Mock interview")


def mode_bar(role: candidate.Role) -> str:
    """Research (search and chat), Prep tools, or the beta Mock interview. Kept per role."""
    key = f"mode_{role.slug}"
    if S.get(key) not in MODES:
        S[key] = MODES[0]
    with st.container(key="modebar"):
        picked = st.segmented_control("Mode", MODES, key=key, label_visibility="collapsed")
    if picked is None:
        S[key] = MODES[0]
        st.rerun()
    return picked


WRITER_LABELS = {"claude": "Claude (your login)", "openai": "OpenAI (API key, billed per use)"}


def _no_login() -> None:
    st.warning("Writing drafts and feedback needs Claude through your Claude Code login (the `claude` command) or an OpenAI key "
               "(OPENAI_API_KEY in .env). Neither is available here.")


def _pick_writer(writers: dict, key: str):
    """Which model writes. Returns (provider or None, ready to go, its name).

    Claude uses your login. OpenAI uses your API key and needs a yes each time, because your CV, About you and the job text are sent to it."""
    ready = {k: v for k, v in writers.items() if v is not None}
    if not ready:
        _no_login()
        return None, False, ""
    name = next(iter(ready))
    if len(ready) > 1:
        name = st.radio("Write with", list(ready), format_func=WRITER_LABELS.get, horizontal=True, key=f"writer_{key}")
    agreed = True
    if name == "openai":
        agreed = st.checkbox("Send my CV, About you, intent and the job description to OpenAI for this. OpenAI bills your API key.",
                             key=f"openai_ok_{key}_{_round('openai_ok')}")
    return ready[name], agreed, name


def prep_page(role: candidate.Role, writers: dict) -> None:
    """Gap analysis, introductions, story bank and likely questions. Each result is saved and can be redone."""
    import prep
    with st.container(key="page_prep"):
        tools = prep.tools_for(role.kind)
        st.caption("Each tool reads your CV/Resume for this role, and About you, your intent and the job description, all in full. "
                   "It only uses what is in them; anything it cannot find is marked GAP for you to fill in.")
        miss = prep.missing_inputs(role)
        if miss:
            st.info("Add " + " and ".join(miss) + " first (Profile page, or the details of this role).")
        complete, agreed, wname = _pick_writer(writers, f"prep_{role.slug}")
        key = st.selectbox("Tool", [t.key for t in tools], format_func=lambda k: prep.TOOLS[k].title, key=f"tool_{role.slug}")
        tool = prep.TOOLS[key]
        st.caption(tool.help)
        saved = prep.load(role, key)
        if st.button("Write it again" if saved else "Write it", key=f"gen_{role.slug}_{key}", disabled=bool(miss) or complete is None or not agreed):
            with st.spinner(f"{'OpenAI' if wname == 'openai' else 'Claude'} is writing. This can take a minute..."):
                ok, msg = prep.run(tool, role, complete)
            if wname == "openai":
                _next_round("openai_ok")                          # asks again next time
            if not ok:
                st.error(msg)
            if ok:
                st.rerun()
        if saved:
            st.markdown(safetext.strip_images(saved))
            note = prep.check(saved, role)
            if note:
                st.warning(note)
            st.download_button("Download as Markdown", saved, file_name=f"{role.slug}-{key}.md", key=f"dl_{role.slug}_{key}")


def mock_page(role: candidate.Role, writers: dict) -> None:
    """A text interview, one question at a time. Voice is planned; this is a beta."""
    import mock
    import prep
    with st.container(key="page_mock"):
        st.markdown("### Mock interview, beta")
        st.caption("Text only for now. You get one question at a time, write your answer, and receive written feedback "
                   "and a stronger version of your answer. No scores.")
        miss = prep.missing_inputs(role)
        if miss:
            st.info("Add " + " and ".join(miss) + " first.")
        key = f"mockstate_{role.slug}"
        state = S.get(key)
        if not state:
            complete, agreed, wname = _pick_writer(writers, f"mock_{role.slug}")
            kind = st.radio("Interview type", list(mock.TYPES), horizontal=True, key=f"mocktype_{role.slug}")
            if st.button("Start", key=f"mockstart_{role.slug}", disabled=bool(miss) or complete is None or not agreed):
                try:
                    with st.spinner("Preparing the first question..."):
                        q = mock.next_question(complete, role, kind, [])
                    S[key] = {"kind": kind, "started": mock.now(), "writer": wname, "items": [{"question": q}]}
                    if wname == "openai":
                        _next_round("openai_ok")
                except Exception as e:
                    st.error(f"Could not start: {e}")
                    return
                st.rerun()
            return
        complete = writers.get(state.get("writer", "claude"))
        if complete is None:
            st.warning("The model that started this interview is no longer available. End it and start again.")
            if st.button("End interview", key=f"mockend_gone_{role.slug}"):
                S.pop(key, None)
                st.rerun()
            return
        items = state["items"]
        cur = items[-1]
        st.markdown(f"**{state['kind']} interview, question {len(items)} of up to {mock.MAX_QUESTIONS}**")
        st.markdown(f"> {cur['question']}")
        if "feedback" not in cur:
            ans = st.text_area("Your answer", height=220, key=f"mockans_{role.slug}_{len(items)}",
                               help="Write it as you would say it. Aim for one to two minutes spoken.")
            if st.button("Get feedback", key=f"mockfb_{role.slug}_{len(items)}", disabled=complete is None):
                try:
                    with st.spinner("Reading your answer..."):
                        cur["answer"] = ans
                        cur["feedback"] = mock.feedback(complete, role, state["kind"], cur["question"], ans)
                    mock.record(role, state["kind"], state["started"], items)
                except Exception as e:
                    cur.pop("answer", None)
                    cur.pop("feedback", None)
                    st.error(str(e))
                    return
                st.rerun()
        else:
            st.markdown("**Your answer**")
            st.write(cur["answer"])
            st.markdown(safetext.strip_images(cur["feedback"]))
            note = prep.check(cur["feedback"], role)
            if note:
                st.warning(note)
            more = len(items) < mock.MAX_QUESTIONS
            c1, c2 = st.columns(2)
            if more and c1.button("Next question", key=f"mocknext_{role.slug}_{len(items)}", use_container_width=True):
                try:
                    with st.spinner("Preparing the next question..."):
                        q = mock.next_question(complete, role, state["kind"], [i["question"] for i in items])
                    items.append({"question": q})
                except Exception as e:
                    st.error(f"Could not get the next question: {e}")
                    return
                st.rerun()
            if c2.button("End interview", key=f"mockend_{role.slug}_{len(items)}", use_container_width=True):
                S.pop(key, None)
                S.flash = (True, "Interview saved in this role's prep folder.")
                st.rerun()
