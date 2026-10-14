"""Web interface (runs on this machine only):  streamlit run app.py

Layout follows the design mockup: sources on the left, conversation in the middle, evidence on the right,
a Morning/Night theme, and a slow, quiet background. Project code never leaves the machine until you
choose a provider in the confirmation dialog.
"""
from __future__ import annotations

import hmac
import html
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import streamlit as st

import settings

settings.setup_env()

from consent import ConsentRequest                     # noqa: E402
from engine import Engine, TurnResult, _claude_login   # noqa: E402
import candidate
import projects
import safetext
import webfetch                                        # noqa: E402
import prep_ui                                          # noqa: E402
import knowledge                                        # noqa: E402
import projectctx                                       # noqa: E402
from index import Index                                # noqa: E402
from providers import build_providers                  # noqa: E402
from providers.claude import CLAUDE_MODELS             # noqa: E402
from providers.fake import FakeProvider                # noqa: E402
from providers.gemini import GEMINI_MODELS             # noqa: E402
from providers.ollama import installed_models          # noqa: E402
from sessions import Session, TurnRecord               # noqa: E402

st.set_page_config(page_title="Interview Prep Tool", page_icon=None, layout="wide")
S = st.session_state

# ------------------------------------------------------------------ theme
LIGHT = dict(bg="#f3efe6", bg2="#e9e3d4", panel="rgba(252,250,244,.80)", fg="#22252b", muted="#666a72",
             line="rgba(34,37,43,.14)", accent="#1F6F6B", accent_fg="#f7f4ec", seal="#b8442d", soft="rgba(31,111,107,.08)",
             local="#1F6F6B", local_bg="#D5E6E4", web="#A8661A", web_bg="#F0E2CF",
             code="#6B4FA0", code_bg="#E3DCF0", model="#666a72", model_bg="transparent")
DARK = dict(bg="#111317", bg2="#0b0c0f", panel="rgba(30,33,40,.78)", fg="#c4c1b8", muted="#8a8e98",
            line="rgba(232,230,223,.13)", accent="#6FC2BB", accent_fg="#111317", seal="#e5806a", soft="rgba(111,194,187,.10)",
            local="#6FC2BB", local_bg="#1B3533", web="#E0A458", web_bg="#3A2D1B",
            code="#B49CE0", code_bg="#2B2440", model="#9a9ea8", model_bg="transparent")

if "theme" not in S:
    hr = time.localtime().tm_hour
    S.theme = "Night" if (hr >= 19 or hr < 6) else "Morning"


ASSETS = Path(__file__).parent / "assets"
GEMINI_GRADIENT = ('<defs><linearGradient id="g" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#4285F4"/>'
                   '<stop offset=".55" stop-color="#9B72CB"/><stop offset="1" stop-color="#D96570"/></linearGradient></defs>')
LINE_ICONS = {
    "auto": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h5l5-6h6M13 18h6M8 12l5 6M17 3l3 3-3 3M16 15l3 3-3 3"/></svg>',
    "compare": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="1.8"><circle cx="9" cy="12" r="6"/><circle cx="15" cy="12" r="6"/></svg>',
}


def _uri(svg: str) -> str:
    import base64
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def icon_css() -> str:
    """Official Claude, Gemini and Ollama marks (Simple Icons files in assets/) for the model switch."""
    def read(n):
        try:
            return (ASSETS / f"{n}.svg").read_text(encoding="utf-8")
        except OSError:
            return None
    claude, gem, oll = read("claude"), read("googlegemini"), read("ollama")
    names = ["Auto", "Claude", "Gemini", "Compare both", "Ollama"]
    mask = lambda svg: f'background:var(--fg); -webkit-mask:url({_uri(svg)}) center/contain no-repeat; mask:url({_uri(svg)}) center/contain no-repeat;'
    pic = lambda svg: f'background:url({_uri(svg)}) center/contain no-repeat;'
    marks = {
        1: pic(claude.replace("<path ", '<path fill="#D97757" ')) if claude else None,
        2: pic(gem.replace("<path ", GEMINI_GRADIENT.replace("<defs>", "<defs>") + '<path fill="url(#g)" ', 1)) if gem else None,
        3: mask(LINE_ICONS["compare"]),
        0: mask(LINE_ICONS["auto"]),
        4: mask(oll) if oll else None,
    }
    out = [
        '.st-key-mode, .st-key-mode > div { width:100%; } .st-key-mode { padding-top:6px; }',
        '.st-key-mode [role="radiogroup"] { width:100%; justify-content:flex-end; gap:14px; flex-wrap:nowrap; }',
        '.st-key-mode [data-testid="stRadioOption"] { position:relative; display:flex; align-items:center; gap:6px; margin:0; padding:0; cursor:pointer; }',
        '.st-key-mode [data-testid="stRadioOption"] > div { order:1; display:flex; align-items:center; }',
        '.st-key-mode [data-testid="stRadioOption"] [data-testid="stMarkdownContainer"] { display:none !important; }',
        '.st-key-mode [data-testid="stRadioOption"]::before { order:2; content:""; width:16px; height:16px; display:block; opacity:.6; transition:opacity .25s ease; }',
        '.st-key-mode [data-testid="stRadioOption"]:hover::before, .st-key-mode [data-testid="stRadioOption"]:has(input:checked)::before { opacity:1; }',
        '.st-key-mode [data-testid="stRadioOption"]::after { position:absolute; top:calc(100% + 2px); left:50%; transform:translateX(-50%); font:11px "IBM Plex Mono",monospace; color:var(--muted); white-space:nowrap; opacity:0; pointer-events:none; transition:opacity .2s ease; z-index:5; }',
        '.st-key-mode [data-testid="stRadioOption"]:hover::after { opacity:1; }',
    ]
    for i, nm in enumerate(names):
        if marks.get(i):
            out.append(f'.st-key-mode [data-testid="stRadioOption"]:has(input[value="{i}"])::before {{ {marks[i]} }}')
        else:
            out.append(f'.st-key-mode [data-testid="stRadioOption"]:has(input[value="{i}"])::before {{ background:var(--fg); -webkit-mask:none; }}')
        out.append(f'.st-key-mode [data-testid="stRadioOption"]:has(input[value="{i}"])::after {{ content:"{nm}"; }}')
    return "\n".join(out)


SUN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
MOON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="black"><path d="M20.5 14.2A8.5 8.5 0 0 1 9.8 3.5a8.5 8.5 0 1 0 10.7 10.7z"/></svg>'


def switch_css() -> str:
    """Morning/Night as a sliding switch: a sun on one side, a moon on the other."""
    m = lambda svg: f"-webkit-mask:url({_uri(svg)}) center/contain no-repeat; mask:url({_uri(svg)}) center/contain no-repeat;"
    t = '.st-key-theme_sw label > div:nth-of-type(1)'
    return "\n".join([
        f'{t} {{ position:relative; width:58px !important; height:30px !important; border-radius:15px !important; background:var(--panel) !important; border:1px solid var(--line) !important; cursor:pointer; transition:border-color .3s ease; }}',
        f'{t}:hover {{ border-color:var(--accent) !important; }}',
        f'{t}::before, {t}::after {{ content:""; position:absolute; top:7px; width:14px; height:14px; background:var(--muted); }}',
        f'{t}::before {{ left:8px; {m(SUN)} }}',
        f'{t}::after {{ right:8px; {m(MOON)} }}',
        f'{t} > div {{ position:absolute !important; top:3px !important; left:3px !important; width:22px !important; height:22px !important; border-radius:50% !important; transform:none !important; background:var(--fg) !important; border:none !important; transition:left .35s cubic-bezier(.2,.7,.2,1); z-index:1; }}',
        f'{t} > div::before {{ content:""; position:absolute; inset:4px; background:var(--bg2); {m(SUN)} }}',
        '.st-key-theme_sw label:has(input:checked) > div:nth-of-type(1) > div { left:31px !important; }',
        f'.st-key-theme_sw label:has(input:checked) > div:nth-of-type(1) > div::before {{ {m(MOON)} }}',
        '.st-key-theme_sw label > div:nth-of-type(2) { display:none !important; }',
        '.st-key-theme_sw { margin-left:auto; margin-top:-5px; }',
    ])


def _rings(cx, cy, phase, n, step, sx=1.3, sy=.9) -> str:
    out = []
    for i in range(1, n + 1):
        r0, pts = step * i + 8, []
        for j in range(121):
            t = 2 * math.pi * j / 120
            r = r0 * (1 + .12 * math.sin(3 * t + phase + i * .09) + .07 * math.sin(5 * t + phase * 2) + .04 * math.sin(2 * t + i * .15))
            pts.append(f"{cx + r * sx * math.cos(t):.0f} {cy + r * sy * math.sin(t):.0f}")
        out.append("M" + " L".join(pts) + "Z")
    return " ".join(out)


def _lines_uri(viewbox: str, d: str, color: str) -> str:
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='{viewbox}' preserveAspectRatio='xMinYMid slice'>"
           f"<path d='{d}' fill='none' stroke='{color}' stroke-opacity='.09' stroke-width='1.2'/></svg>")
    return 'url("data:image/svg+xml,' + quote(svg, safe="/:=' ") + '")'


def background_css(night: bool) -> str:
    """A teal-to-sand wash, with contour lines down the left side and along the bottom (drawn once, fixed to the screen)."""
    left = _rings(-20, 430, .8, 7, 26, 1.0, 1.0) + " " + _rings(-30, 880, 3.4, 7, 24, 1.05, 1.0)
    bottom = _rings(900, 430, 2.2, 19, 21, 1.5, .9)
    ink = "#c4c1b8" if night else "#1a2230"
    teal, sand = ("111,194,187,.10", "120,100,70,.12") if night else ("31,111,107,.10", "196,164,110,.12")
    wash = f"linear-gradient(180deg,rgba({teal}) 0,transparent 36%,rgba({sand}) 100%)"
    return (f".stApp {{ background-color:var(--bg); background-repeat:no-repeat; background-attachment:fixed; "
            f"background-size:100% 100%,auto 100%,100% auto; background-position:0 0,left top,center bottom; "
            f"background-image:{wash},{_lines_uri('0 0 260 1000', left, ink)},{_lines_uri('0 0 1600 400', bottom, ink)}; }}")


def set_sources(values: dict):
    """Tick or untick several sources at once (runs as a button callback, before the checkboxes are drawn)."""
    for key, value in values.items():
        S[key] = value


def set_theme():
    S.theme = "Night" if S.theme_sw else "Morning"


def css(t: dict, scheme: str = "light") -> str:
    v = ";".join(f"--{k.replace('_', '-')}:{val}" for k, val in t.items())
    return f"""
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=Literata:ital,opsz,wght@0,7..72,400;0,7..72,500;1,7..72,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{ {v}; }}
html, body, .stApp {{ font-family: "Instrument Sans", system-ui, sans-serif; color-scheme: {scheme}; }}
.stMarkdown, .stMarkdown p, .stMarkdown li, button p, label p, input, textarea, [data-testid="stWidgetLabel"] p, [data-testid="stCaptionContainer"] {{ font-family: "Instrument Sans", system-ui, sans-serif; }}
.stApp {{ background-color: var(--bg); color: var(--fg); }}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ position: relative; z-index: 1; max-width: min(2000px, 96vw); padding-top: 4.2rem; padding-left: 2rem; padding-right: 2rem; }}
[data-testid="stDialog"] :is(h1, h2, h3):not([data-testid="stMarkdownContainer"] *) {{ padding: 16px 24px 0 !important; font-size: 15px !important; color: var(--fg) !important; }}
[data-testid="stColumn"]:has(.st-key-side_left), [data-testid="stColumn"]:has(.st-key-side_right) {{ position: sticky; top: 4.4rem; align-self: flex-start;
  max-height: calc(100vh - 5.4rem); overflow-y: auto; overflow-x: hidden; scrollbar-width: thin; }}
[data-testid="stSpinner"], [data-testid="stSpinner"] > div, [data-testid="stSpinner"] * {{ background: transparent !important; color: var(--muted) !important; }}
div:has(> .st-key-convbar) {{ position: sticky; top: 2.7rem; z-index: 20; }}
[data-testid="stColumn"] [data-testid="stVerticalBlock"].st-key-convbar {{ background: var(--bg) !important; padding: .5rem 0 .4rem; border-bottom: 1px solid var(--line); }}
.st-key-convbar h3 {{ padding: 0 !important; }}
@media (max-width: 900px) {{
  div:has(> .st-key-convbar) {{ top: 2.6rem; }}
  .st-key-convbar [data-testid="stHorizontalBlock"] {{ flex-wrap: wrap !important; gap: .3rem 1rem; }}
  .st-key-convbar [data-testid="stColumn"] {{ min-width: max-content !important; flex: 0 0 auto !important; }}
  [data-testid="stColumn"]:has(.st-key-side_left), [data-testid="stColumn"]:has(.st-key-side_right) {{ position: static; max-height: none; overflow: visible; }}
  [data-testid="stColumn"]:has(.st-key-side_mid) {{ order: 0; }}
  [data-testid="stColumn"]:has(.st-key-side_right) {{ order: 1; }}
  [data-testid="stColumn"]:has(.st-key-side_left) {{ order: 2; }}
  [data-testid="stHorizontalBlock"]:not(:has(> [data-testid="stColumn"] .st-key-side_mid)) {{ flex-wrap: nowrap !important; }}
  [data-testid="stHorizontalBlock"]:not(:has(> [data-testid="stColumn"] .st-key-side_mid)) > [data-testid="stColumn"] {{ min-width: 0 !important; width: auto !important; }}
  .block-container {{ padding-left: 1rem; padding-right: 1rem; padding-top: 3.4rem; }}
  .st-key-mode [role="radiogroup"] {{ justify-content: space-between; gap: 6px; }}
  .st-key-toggles [data-testid="stHorizontalBlock"] {{ flex-wrap: wrap !important; gap: .2rem 1.2rem; }}
  .st-key-toggles [data-testid="stColumn"] {{ min-width: max-content !important; flex: 0 0 auto !important; }}
  .st-key-side_mid h3 {{ display: none; }}
  .st-key-answers.st-key-answers [data-testid="stHorizontalBlock"] {{ flex-wrap: wrap !important; row-gap: .3rem; }}
  .st-key-answers [data-testid="stColumn"] {{ min-width: 0 !important; flex: 0 1 auto !important; max-width: 100%; }}
  [class*="st-key-cite_"], [class*="st-key-cite_"] button {{ max-width: 100%; }}
  [class*="st-key-cite_"] button {{ height: auto; white-space: normal; text-align: left; }}
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] .st-key-toggles) {{ flex-wrap: wrap !important; row-gap: .3rem; }}
  [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] .st-key-toggles) > [data-testid="stColumn"] {{ flex: 1 1 100% !important; }}
}}
.brand {{ font-family: "Literata", Georgia, serif !important; font-weight: 400; color: var(--fg); }}
h1, h2, h3 {{ font-family: "Instrument Sans", system-ui, sans-serif !important; font-weight: 600; font-size: 12.5px !important; letter-spacing: .03em; color: var(--muted) !important; padding: 0 0 8px !important; }}
.stMarkdown, .stMarkdown p, .stMarkdown li, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3, [data-testid="stWidgetLabel"] p,
[data-testid="stExpander"] summary p, [data-testid="stCheckbox"] label p, [data-testid="stRadio"] label p, [data-testid="stToggle"] label p,
[data-testid="stSelectbox"] label p, [data-testid="stTextInput"] label p {{ color: var(--fg); }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{ color: var(--muted) !important; }}
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] {{ background: var(--panel) !important; border-color: var(--line) !important; }}
input, textarea, [data-baseweb="select"] div {{ color: var(--fg) !important; }}
[data-baseweb="popover"] ul, [data-baseweb="menu"] {{ background: var(--bg2) !important; }}
[data-baseweb="popover"] li, [data-baseweb="menu"] li {{ color: var(--fg) !important; background: transparent !important; }}
[data-testid="stCode"] pre, [data-testid="stCode"] code, .stCodeBlock pre {{ background: var(--soft) !important; color: var(--fg) !important; }}
[data-testid="stExpander"] details {{ background: transparent; border-color: var(--line); }}
[data-testid="stBottom"], [data-testid="stBottom"] > div, [data-testid="stBottomBlockContainer"] {{ background: transparent !important; }}
[data-testid="stChatInput"] *, [data-testid="stChatInput"] [data-baseweb] {{ background-color: transparent !important; }}
[data-testid="stChatInput"] textarea {{ color: var(--fg) !important; -webkit-text-fill-color: var(--fg) !important; caret-color: var(--fg); }}
[data-testid="stChatInput"] textarea::placeholder {{ color: var(--muted) !important; -webkit-text-fill-color: var(--muted) !important; }}
[data-testid="stChatInput"] button svg {{ fill: var(--muted); color: var(--muted); }}
[data-testid="stFileUploaderDropzone"] {{ background: var(--panel); border-color: var(--line); }}
[data-testid="stFileUploaderDropzone"] * {{ color: var(--fg); }}
[data-testid="stDialog"] div[role="dialog"], [data-baseweb="modal"] > div > div, [data-testid="stTooltipContent"], [data-baseweb="tooltip"] > div {{ background: var(--bg2) !important; color: var(--fg) !important; }}
[data-testid="stDialog"] *, [data-testid="stTooltipContent"] * {{ color: var(--fg); }}
[data-testid="stAlert"] {{ background: var(--soft) !important; border: 1px solid var(--line); }}
[data-testid="stAlert"] * {{ color: var(--fg) !important; }}
[data-testid="stExpander"] summary, [data-testid="stExpander"] summary:hover {{ background: transparent !important; color: var(--fg); }}
[data-testid="stExpander"] summary svg, [data-testid="stSelectbox"] svg {{ fill: var(--muted); color: var(--muted); }}
[data-testid="stTabs"] button, [data-testid="stTabs"] button p {{ color: var(--muted); }}
[data-testid="stTabs"] button[aria-selected="true"], [data-testid="stTabs"] button[aria-selected="true"] p {{ color: var(--fg); }}
[data-testid="stCheckbox"] span, [data-testid="stToggle"] span {{ color: var(--fg); }}
[data-baseweb="checkbox"] > div:first-child, [data-baseweb="radio"] > div:first-child {{ border-color: var(--muted) !important; }}
[data-baseweb="checkbox"] > div:first-child > div, [data-baseweb="radio"] > div:first-child > div {{ background-color: var(--fg) !important; }}
[data-testid="stFileUploaderDropzone"] button {{ background: transparent; border: 1px solid var(--line); }}
.stButton > button p, .stDownloadButton > button p {{ color: inherit; }}
::selection {{ background: var(--soft); }}
[data-baseweb="select"] > div, [data-baseweb="select"] [role="combobox"], [data-baseweb="input"] > div, [data-baseweb="base-input"] {{ background: var(--panel) !important; }}
[data-baseweb="select"] *, [data-baseweb="input"] *, [data-testid="stSelectbox"] *, [data-testid="stTextInput"] * {{ color: var(--fg) !important; -webkit-text-fill-color: var(--fg) !important; }}
[data-baseweb="select"] svg, [data-testid="stTextInput"] svg {{ fill: var(--muted) !important; }}
[data-testid^="stBaseButton"], [data-testid^="stBaseButton"]:disabled {{ background: transparent !important; color: var(--fg) !important; border: 1px solid var(--line) !important; }}
[data-testid^="stBaseButton"]:disabled {{ opacity: .6; }}
[data-testid^="stBaseButton"] * {{ color: var(--fg) !important; }}
[data-testid^="stBaseButton"]:hover:not(:disabled) {{ border-color: var(--accent) !important; background: var(--soft) !important; }}
[data-testid="stBaseButton-primary"] {{ background: var(--accent) !important; }}
[data-testid="stBaseButton-primary"] * {{ color: var(--accent-fg) !important; }}
[data-testid="stBaseButton-tertiary"] {{ border: none !important; }}
label[data-baseweb="checkbox"]:has(input:not(:checked)) > span:first-child {{ background: var(--bg2) !important; border-color: var(--muted) !important; }}
label[data-baseweb="radio"]:has(input:not(:checked)) > div:first-child {{ background: var(--bg2) !important; border-color: var(--muted) !important; }}
label[data-baseweb="radio"]:has(input:checked) > div:first-child > div {{ background: var(--fg) !important; }}
[data-testid="stToggle"] [data-baseweb="checkbox"] > div:last-child, [data-testid="stToggle"] [role="checkbox"] + div {{ background: var(--fg) !important; }}
[data-testid="stFileUploaderDropzone"], [data-testid="stFileUploaderDropzone"] > div {{ background: var(--panel) !important; }}
[data-testid="stFileUploaderDropzone"] small, [data-testid="stFileUploaderDropzone"] span {{ color: var(--muted) !important; }}
[data-testid="stExpanderDetails"], [data-testid="stExpander"] details > div {{ background: transparent !important; }}
[data-testid="stSelectbox"] [role="group"], [data-testid="stTextInputRootElement"], [data-testid="stSelectbox"] [data-baseweb="select"] > div > div {{ background: var(--panel) !important; border-color: var(--line) !important; }}
[data-testid="stRadioOption"]:not(:has(input:checked)) div:has(> div:empty), [data-testid="stRadioOption"]:not(:has(input:checked)) div:not(:has(*)), [data-testid="stCheckbox"] label:not(:has(input:checked)) div:not(:has(*)) {{ background: var(--bg2) !important; border-color: var(--muted) !important; }}
[data-testid="stToggle"] label div:not(:has(*)) {{ background: var(--fg) !important; }}
blockquote {{ border-left: 2px solid var(--accent); color: var(--fg); }}
[data-testid="stColumn"] [data-testid="stVerticalBlock"] {{ border-color: transparent !important; border-radius: 0 !important; background: transparent !important; box-shadow: none !important; }}
[data-testid="stVerticalBlockBorderWrapper"] {{ background: transparent !important; border: 0 !important; border-radius: 0 !important; backdrop-filter: none !important; box-shadow: none !important; padding: 0 .4rem; margin-bottom: 1.6rem; }}
.stButton > button, .stDownloadButton > button {{ background: transparent; color: var(--fg); border: 1px solid var(--line); border-radius: 6px; transition: border-color .2s ease; }}
.stButton > button:hover {{ border-color: var(--accent); background: var(--soft); color: var(--fg); }}
.stButton > button[kind="primary"] {{ background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }}
[data-testid="stChatInput"] {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }}
.brand {{ display:flex; align-items:center; gap:12px; font-size:20px; letter-spacing:.01em; }}
.lbl {{ display:block; font-size:12px; font-weight:600; color:var(--muted); letter-spacing:.02em; margin:14px 0 4px; }}
.q {{ font-family:"Literata",Georgia,serif; font-weight:500; font-size:18px; line-height:1.4; max-width:62ch; margin:4px 0 6px; }}
.route {{ font-size:12.5px; color:var(--muted); margin-bottom:14px; }}
.chip {{ display:inline-flex; align-items:center; gap:5px; font-weight:600; font-size:11.5px; line-height:1; border-radius:4px; padding:3px 7px 3px 6px; margin:0 2px; vertical-align:.1em; white-space:normal; overflow-wrap:anywhere; }}
.chip::before {{ content:""; width:6px; height:6px; border-radius:50%; background:currentColor; flex:none; }}
.chip.code {{ font-family:"IBM Plex Mono",monospace; font-weight:500; }} .chip.code::before {{ border-radius:1px; }}
.chip.web::before {{ border-radius:0; clip-path:polygon(50% 0,100% 100%,0 100%); }}
.chip.model::before {{ background:transparent; border:1.5px dashed currentColor; }}
.chip.local {{ background:var(--local-bg); color:var(--local); }}
.chip.web {{ background:var(--web-bg); color:var(--web); }}
.chip.code {{ background:var(--code-bg); color:var(--code); }}
.chip.model {{ background:transparent; color:var(--model); outline:1px dashed var(--model); outline-offset:-1px; }}
.st-key-answers [data-testid="stMarkdownContainer"] p {{ font-family:"Literata",Georgia,serif; font-size:17px; line-height:1.75; }}
.st-key-answers [data-testid="stMarkdownContainer"] p:has(.lbl) {{ font-family:"Instrument Sans",system-ui,sans-serif; font-size:inherit; line-height:1.4; }}
.st-key-answers .chip {{ font-family:"Instrument Sans",system-ui,sans-serif; }} .st-key-answers .chip.code {{ font-family:"IBM Plex Mono",monospace; }}
.st-key-answers [data-testid="stExpander"] [data-testid="stMarkdownContainer"] p {{ font-family:"Instrument Sans",system-ui,sans-serif; font-size:14px; line-height:1.5; }}
.chip.unverified {{ background:rgba(184,68,45,.12); color:var(--seal); }}
[class*="st-key-cite_"] {{ width:auto !important; flex:0 0 auto; }}
[class*="st-key-cite_"] button {{ min-height:0 !important; height:auto; width:auto; gap:5px; font-weight:600; border-radius:4px !important; padding:4px 8px 4px 7px !important; border:1px solid transparent !important; }}
[class*="st-key-cite_"] button p {{ font:600 11.5px/1 "Instrument Sans",system-ui,sans-serif !important; }}
[class*="st-key-cite_"] button p::before {{ content:""; display:inline-block; width:6px; height:6px; margin-right:5px; border-radius:50%; background:currentColor; vertical-align:.05em; }}
[class*="st-key-cite_code_"] button p {{ font:500 11.5px/1 "IBM Plex Mono",monospace !important; }}
[class*="st-key-cite_code_"] button p::before {{ border-radius:1px; }}
[class*="st-key-cite_web_"] button p::before {{ border-radius:0; clip-path:polygon(50% 0,100% 100%,0 100%); }}
[class*="st-key-cite_model_"] button p::before {{ background:transparent; border:1.5px dashed currentColor; }}
[class*="st-key-cite_local_"] button {{ background:var(--local-bg) !important; }} [class*="st-key-cite_local_"] button * {{ color:var(--local) !important; }}
[class*="st-key-cite_web_"] button {{ background:var(--web-bg) !important; }} [class*="st-key-cite_web_"] button * {{ color:var(--web) !important; }}
[class*="st-key-cite_code_"] button {{ background:var(--code-bg) !important; }} [class*="st-key-cite_code_"] button * {{ color:var(--code) !important; }}
[class*="st-key-cite_model_"] button {{ outline:1px dashed var(--model); outline-offset:-1px; }} [class*="st-key-cite_model_"] button * {{ color:var(--model) !important; }}
[class*="st-key-cite_unverified_"] button {{ background:rgba(184,68,45,.12) !important; }} [class*="st-key-cite_unverified_"] button * {{ color:var(--seal) !important; }}
[class*="_sel_"] button {{ box-shadow:0 0 0 2px var(--bg), 0 0 0 3.5px var(--fg); }}
[class*="st-key-cite_"] button:hover {{ filter:brightness(.95); }}
.note {{ border-left:2px solid var(--seal); background:rgba(184,68,45,.07); padding:8px 12px; font-size:13px; margin:6px 0; }}
.flow {{ height:1px; background:linear-gradient(90deg,transparent,var(--line) 20%,var(--line) 80%,transparent); margin:14px 0; }}
@media (prefers-reduced-motion: reduce) {{ *, *::before, *::after {{ animation:none !important; transition:none !important; }} }}
</style>
"""


# ------------------------------------------------------------------ resources
@st.cache_resource(show_spinner="Loading the index...")
def load_index(version: int):
    cfg = settings.load_config()
    return cfg, Index.ensure(cfg)


def rebuild_index():
    cfg = settings.load_config()
    Index.build(cfg)
    S.index_version = S.get("index_version", 0) + 1
    load_index.clear()


def make_engine(cfg, index) -> Engine:
    if S.get("demo"):
        provs = {"claude": FakeProvider(label="claude", locality="cloud"), "gemini": FakeProvider(label="gemini", locality="cloud"),
                 "ollama": FakeProvider(label="ollama", locality="local")}
    else:
        provs = build_providers(cfg, {"claude": S.get("claude_model"), "claude_access": S.get("claude_access"), "gemini": S.get("gemini_model"), "ollama": S.get("ollama_model")})
    return Engine(cfg, index, provs)


class NeedConsent(Exception):
    def __init__(self, req: ConsentRequest):
        self.req = req


def ui_consent(req: ConsentRequest):
    """Raise until the user has chosen in the dialog. The answer is tied to the pending question and used once."""
    ans = S.get("consent_answer")
    if not ans or ans.get("id") != S.pending["id"]:
        raise NeedConsent(req)
    return ans["providers"] or None


# ------------------------------------------------------------------ rendering helpers
def short(seg: dict) -> str:
    k, t = seg["kind"], seg["text"]
    if k == "web":
        return urlparse(t).netloc or t
    if k == "model":
        return "model knowledge"
    if k == "code":
        if "(project knowledge)" in t:
            return f"{t.split('/')[0]} knowledge"
        if t.endswith("(project map)"):
            return f"{t.split('/')[0]} map"
        if t.endswith("(data files)"):
            return f"{t.split('/')[0]} data files"
        if not re.search(r":\d+-\d+$", t):
            return Path(t).name                      # a file card
        name, _, lines = t.rpartition(":")
        return f"{Path(name).name}:{lines}"
    if k == "local":
        name, _, rest = t.partition(", ")
        return f"{Path(name).name}{', ' + rest if rest else ''}"
    return t


def answer_markdown(segs: list[dict]) -> str:
    out = []
    for s in segs:
        if s["kind"] == "text":
            out.append(re.sub(r"<(?=[a-zA-Z/!])", "&lt;", safetext.strip_images(s["text"])))
        else:
            out.append(f' <span class="chip {s["kind"]}" title="{html.escape(s["text"])}">{html.escape(chip_label(s))}</span> ')
    return "".join(out)


def cite_items(segs: list[dict]) -> list[dict]:
    """Distinct marks of an answer in order of first use (every kind except plain text)."""
    seen, out = set(), []
    for s in segs:
        key = (s["kind"], s.get("ref") or s["text"])
        if s["kind"] != "text" and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def chip_label(seg: dict) -> str:
    pre = {"local": "local: ", "code": "code: ", "web": "web: ", "unverified": "unverified: "}.get(seg["kind"], "")
    return pre + short(seg)


def same_mark(a: dict | None, b: dict) -> bool:
    return bool(a) and (a["kind"], a.get("ref") or a["text"]) == (b["kind"], b.get("ref") or b["text"])


def pick_evidence(seg: dict):
    S.evidence = seg  # runs before the rerun, so the right column already shows it


def to_view(res: TurnResult) -> dict:
    return {"question": res.question, "route": res.route.reason if res.route else "", "notes": res.notes,
            "cancelled": res.cancelled, "image": res.image_path,
            "answers": [{"provider": a.provider, "model": a.model, "error": a.error,
                         "segments": [vars(s) for s in a.segments]} for a in res.answers]}


def record_to_view(t: TurnRecord) -> dict:
    return {"question": t.question, "route": t.route, "notes": t.notes, "cancelled": False, "image": t.image,
            "answers": [{"provider": a["provider"], "model": a.get("model", ""), "error": a.get("error"), "segments": a.get("segments", [])}
                        for a in t.answers]}


def show_view(v: dict, idx: int, chunk_by_id: dict):
    st.markdown(f'<div class="q">{html.escape(v["question"])}</div>', unsafe_allow_html=True)
    if v["route"]:
        st.markdown(f'<div class="route">{html.escape(v["route"])}</div>', unsafe_allow_html=True)
    for n in v["notes"]:
        st.markdown(f'<div class="note">{html.escape(n)}</div>', unsafe_allow_html=True)
    answers = v["answers"]
    cols = st.columns(len(answers)) if len(answers) > 1 else [st.container()]
    for ai, (col, a) in enumerate(zip(cols, answers)):
        with col:
            st.markdown(f'<span class="lbl">{html.escape(a["provider"])} · {html.escape(a["model"])}</span>', unsafe_allow_html=True)
            if a["error"]:
                st.markdown(f'<div class="note">{html.escape(a["error"])}</div>', unsafe_allow_html=True)
                continue
            st.markdown(answer_markdown(a["segments"]), unsafe_allow_html=True)
            cites = cite_items(a["segments"])
            if cites:  # clickable copies of the marks, styled as chips (.st-key-cite_*)
                with st.container(horizontal=True, gap="small"):
                    for ci, c in enumerate(cites):
                        sel = "sel" if same_mark(S.get("evidence"), c) else "off"
                        st.button(chip_label(c), key=f"cite_{c['kind']}_{sel}_{idx}_{ai}_{ci}", help=c["text"],
                                  on_click=pick_evidence, args=(c,))
    if v.get("image") and Path(v["image"]).exists():
        st.image(v["image"], caption="AI-generated draft. Check small text and numbers against the sources.")
    st.markdown('<div class="flow"></div>', unsafe_allow_html=True)


def show_evidence(chunk_by_id: dict):
    s = S.get("evidence")
    if not s:
        st.caption("Click a mark under an answer to see the exact passage here. A tag shows that a source exists, not that it supports the claim, so check anything important.")
        return
    if s["kind"] == "model":
        st.caption("From the model's own knowledge, no source found.")
        return
    if s["kind"] == "unverified":
        st.caption("This id was not in the retrieved sources.")
        return
    if s["kind"] == "web":
        st.markdown(f"[{s['text']}]({s['text']})")
        st.caption("Web page used by the model. Opens in a new tab.")
        return
    if str(s.get("ref") or "").startswith("explore:"):
        label = s["ref"].split(":", 1)[1]
        st.markdown(f'<div class="route">{html.escape(label)}</div>', unsafe_allow_html=True)
        if s.get("detail"):
            st.code(s["detail"], language=Path(label.rpartition(":")[0]).suffix.lstrip(".") or None, wrap_lines=True)
            st.caption("Lines Claude read in a temporary copy of the project, kept with this chat. The line numbers refer to the copy.")
        else:
            st.caption("This passage was read in a temporary copy of the project that has since been deleted.")
        return
    c = chunk_by_id.get(s["ref"])
    if not c:
        st.caption("This passage is no longer in the index (it may have been re-indexed).")
        return
    st.markdown(f'<div class="route">{html.escape(c.label())}</div>', unsafe_allow_html=True)
    if c.kind == "code" and (c.section or "").startswith(knowledge.KNOWLEDGE_PREFIX):
        st.markdown(c.text)
        st.caption("Written by Claude from the project's code, earlier. It gives the overall picture; check exact details against the code.")
    elif c.kind == "code" and c.section in ("project map", "file card", "data files"):
        st.code(c.text, language=None, wrap_lines=True)
        st.caption("A summary built from the project's structure. Calls and imports are matched by name and are approximate.")
    elif c.kind == "code":
        st.code(c.text, language=Path(c.source).suffix.lstrip(".") or None, line_numbers=True)
    else:
        st.markdown(f"> {c.text[:1800]}")


@st.dialog("This question will send project code to a model")
def consent_dialog(req: ConsentRequest):
    st.write("Files that would be sent:")
    if req.detail:
        st.info(req.detail)
    st.code("\n".join(req.files[:15]) + (f"\n... and {len(req.files) - 15} more" if len(req.files) > 15 else ""), language=None)
    if req.carried:
        st.caption("Code from an earlier turn in this chat is still part of the context.")
    opts = [o for o in req.options]
    labels = {o.key: o.label + ("" if o.available else f"  (unavailable: {o.note})") for o in opts}
    avail = [o.key for o in opts if o.available]
    if req.compare:
        chosen = st.multiselect("Which providers may receive it?", avail, format_func=lambda k: labels[k])
    else:
        pick = st.radio("Which provider may receive it?", avail, index=None, format_func=lambda k: labels[k])
        chosen = [pick] if pick else []
    c1, c2 = st.columns(2)
    if c1.button("Send", type="primary", disabled=not chosen, use_container_width=True):
        S.consent_answer = {"id": S.pending["id"], "providers": chosen}
        st.rerun()
    if c2.button("Cancel, send nothing", use_container_width=True):
        S.consent_answer = {"id": S.pending["id"], "providers": []}
        st.rerun()


@st.dialog("Project knowledge", width="large")
def knowledge_dialog(name: str, update: bool):
    req0, _ = eng.knowledge_request(name, update)
    if req0 is None:
        st.error("This project has no readable files.")
        return
    avail = [o for o in req0.options if o.available]
    labels = {o.key: o.label for o in req0.options}
    st.caption("A model reads the files and writes a document about the project. A large project is read in several parts, a few at a time. "
               "Later questions use the matching parts of it automatically. "
               "The document stays on this machine (knowledge/) and is never committed. Every file and line it cites is checked against the code.")
    pick = st.radio("Which provider may read the code?", [o.key for o in avail], index=None, format_func=labels.get, key="kb_provider",
                    help="Claude reads it through your Claude Code login and gives the best result. An Ollama model reads it on this machine "
                         "(unless it is a :cloud model), slower and less reliably. Gemini cannot do this.")
    if not avail:
        st.error("Neither Claude (through your login) nor Ollama is available. Install and log in to Claude Code, or start Ollama with a model.")
    if pick:
        req, send = eng.knowledge_request(name, update, pick)
        st.info(req.detail)
        st.code("\n".join(req.files[:15]) + (f"\n... and {len(req.files) - 15} more" if len(req.files) > 15 else ""), language=None)
    else:
        st.caption("Choose a provider to see what would be sent. Nothing is sent until you press the button.")
    c1, c2 = st.columns(2)
    if c1.button("Update" if update else "Build", type="primary", use_container_width=True, disabled=not pick):
        bar = st.progress(0.0, text="Starting...")

        def on_progress(done: int, total: int, label: str):
            bar.progress(min(done / max(total, 1), 1.0), text=f"Reading {label} ({done} of {total} steps)")
        S.kb_msg = eng.build_knowledge(name, consent=lambda r: [pick], update=update, progress=on_progress, provider=pick)
        st.rerun()
    if c2.button("Cancel, send nothing", use_container_width=True):
        st.rerun()


@st.cache_resource
def _attempts(code: str) -> dict:
    """Wrong-passcode counter shared by every browser tab, so reloading the page does not reset it."""
    return {"fails": 0, "until": 0.0}


def passcode_gate():
    """With a passcode set, nothing is shown until it is entered. Opening the app to other devices requires one."""
    code = os.environ.get("INTERVIEW_PREP_PASSCODE", "")
    if os.environ.get("INTERVIEW_PREP_LAN") == "1" and not code:
        st.error("This server accepts connections from other devices, so it needs a passcode. Add INTERVIEW_PREP_PASSCODE=... to .env and restart.")
        st.stop()
    if not code or S.get("authed"):
        return
    st.markdown('<div class="brand">Interview Prep Tool</div>', unsafe_allow_html=True)
    with st.form("passcode_form"):
        entry = st.text_input("Passcode", type="password", placeholder="Passcode", label_visibility="collapsed", key="passcode_entry")
        go = st.form_submit_button("Open")
    if go:
        a = _attempts(code)
        wait = int(a["until"] - time.time())
        if wait > 0:
            st.error(f"Too many wrong attempts. Try again in {wait} seconds.")
        elif hmac.compare_digest(entry.encode(), code.encode()):
            a["fails"] = 0
            S.authed = True
            st.rerun()
        else:
            a["fails"] += 1
            if a["fails"] >= 5:
                a["fails"], a["until"] = 0, time.time() + 60
            st.error("That passcode is not right.")
    st.stop()


PREVIEW_CHARS, PREVIEW_PAGES = 40_000, 15


def read_preview(path: Path) -> tuple[str, str]:
    """Text of a source for the preview window, and a note when it was cut short."""
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        parts, total = [], 0
        for n, page in enumerate(reader.pages[:PREVIEW_PAGES], start=1):
            text = (page.extract_text() or "").strip()
            parts.append(f"**Page {n}**\n\n{text}")
            total += len(text)
            if total > PREVIEW_CHARS:
                break
        shown = len(parts)
        note = f"Showing the first {shown} of {len(reader.pages)} pages." if shown < len(reader.pages) else ""
        return "\n\n---\n\n".join(parts), note
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:PREVIEW_CHARS], ("Showing the first part of the file." if len(text) > PREVIEW_CHARS else "")


@st.dialog("Preview", width="large")
def preview_dialog(path: Path, name: str):
    st.caption(name)
    try:
        text, note = read_preview(path)
    except Exception as e:
        st.error(f"Could not read this file: {e}")
        return
    with st.container(height=520):
        if not text.strip():
            st.caption("No text could be read from this file (a scanned PDF has no text layer).")
        elif path.suffix.lower() == ".txt":
            st.code(text, language=None, wrap_lines=True)
        else:
            st.markdown(text)
    if note:
        st.caption(note)


# ------------------------------------------------------------------ page
_html = css(DARK, "dark") if S.theme == "Night" else css(LIGHT, "light")
st.markdown(_html.replace("</style>", icon_css() + "\n" + switch_css() + "\n" + prep_ui.css() + "\n" + background_css(S.theme == "Night") + "\n</style>", 1), unsafe_allow_html=True)
passcode_gate()                       # nothing below runs, and no file is read, until the passcode is entered

S.setdefault("index_version", 0)
cfg, index = load_index(S.index_version)
chunk_by_id = {c.id: c for c in index.chunks}
S.setdefault("views", [])
S.setdefault("pending", None)
S.setdefault("selected", None)

# top bar
b1, b2 = st.columns([2.2, 2])
b1.markdown('<div class="brand">Interview Prep Tool</div>', unsafe_allow_html=True)
with b2.container(key="toggles"):
    S.setdefault("theme_sw", S.theme == "Night")
    st.toggle("Night", key="theme_sw", on_change=set_theme, help="Morning or Night")

view = prep_ui.view_bar()
role = candidate.get_role(view) if view not in (prep_ui.PROFILE, prep_ui.NEW_ROLE) else None
ROLE = role.slug if role else ""
cfg = candidate.configured(cfg, role)
eng = make_engine(cfg, index)

if view == prep_ui.PROFILE:
    prep_ui.profile_page(_claude_login(eng.providers.get("claude")), rebuild_index)
    st.stop()
if view == prep_ui.NEW_ROLE:
    prep_ui.new_role_page(rebuild_index)
    st.stop()

if S.get("active_role") != ROLE or "session" not in S:       # each role has its own chat
    S.active_role = ROLE
    S.session, S.views, S.pending, S.consent_answer, S.evidence = Session(role=ROLE), [], None, None, None
prep_ui.role_details(role, rebuild_index)
mode = prep_ui.mode_bar(role)
if mode == "Prep tools":
    prep_ui.prep_page(role, _claude_login(eng.providers.get("claude")))
    st.stop()
if mode == "Mock interview":
    prep_ui.mock_page(role, _claude_login(eng.providers.get("claude")))
    st.stop()

left, mid, right = st.columns([1.05, 2.5, 1.25], gap="medium")

# ---- left: sources
with left:
    with st.container(key="side_left"):
        with st.container(border=True):
            st.markdown("### Sources")
            groups = {g: i for g, i in index.groups().items() if candidate.visible(g, ROLE)}
            docs = sorted(g for g, i in groups.items() if i["kind"] == "local")
            projs = [p["name"] for p in cfg["projects"] if p["name"] in groups]
            sel: set[str] = set()
            st.markdown('<span class="lbl">Documents</span>', unsafe_allow_html=True)
            gone = settings.removed_sources()
            names = [Path(d).name for d in docs]
            nice = lambda g: candidate.source_label(g) if candidate.is_workspace(g) else (
                Path(g).name if names.count(Path(g).name) == 1 else "/".join(Path(g).parts[-2:]))
            shown = sorted((d for d in docs if d not in gone), key=lambda g: (not candidate.is_workspace(g), g))   # your own documents first
            a1, a2 = st.columns(2)
            a1.button("Select all", key="sel-all", use_container_width=True, on_click=set_sources, args=({f"src-{d}": True for d in shown},),
                      help="Tick every paper and note. Project code is never ticked for you.")
            a2.button("Clear all", key="sel-none", use_container_width=True, on_click=set_sources,
                      args=({f"src-{g}": False for g in shown + projs},), help="Untick everything, including project code.")
            for g in shown:
                r1, r2, r3 = st.columns([6, 1, 1], vertical_alignment="center")
                if r1.checkbox(nice(g), value=True, key=f"src-{g}", help=g):
                    sel.add(g)
                if r2.button("", key=f"pv-{g}", icon=":material/visibility:", help="Preview this source", type="tertiary"):
                    f = settings.group_path(cfg, g)
                    if f:
                        preview_dialog(f, g)
                    else:
                        st.toast("This file is no longer on disk.")
                if candidate.is_workspace(g):
                    r3.empty()                    # managed on the Profile page and in the role's details
                elif r3.button("", key=f"rm-{g}", icon=":material/delete:", help="Remove this source", type="tertiary"):
                    S.confirm_remove = g
            g = S.get("confirm_remove")
            if g:
                added = g.startswith("library/added/")
                st.warning(f"Remove **{Path(g).name}**? " + ("It was added through this app, so the file is deleted too." if added
                           else "The file stays on disk; it is just no longer searched. You can restore it below."))
                y, n = st.columns(2)
                if y.button("Remove", key="rm-yes", use_container_width=True):
                    if added:
                        f = settings.assert_writable(settings.ROOT / g)
                        f.unlink(missing_ok=True)
                    else:
                        settings.set_removed_sources(settings.removed_sources() | {g})
                    S.confirm_remove = None
                    if added:
                        with st.spinner("Re-indexing..."):
                            rebuild_index()
                    st.rerun()
                if n.button("Keep", key="rm-no", use_container_width=True):
                    S.confirm_remove = None
                    st.rerun()
            if gone:
                with st.expander(f"Removed ({len(gone)})"):
                    for g in sorted(gone):
                        if st.button(f"Restore {Path(g).name}", key=f"rs-{g}", use_container_width=True):
                            settings.set_removed_sources(gone - {g})
                            st.rerun()
            st.markdown('<span class="lbl">Project code, read-only</span>', unsafe_allow_html=True)
            mine = set(projects.local_names()) if not projects.lan_mode() else set()
            for g in projs:
                r1, r2 = st.columns([8, 1], vertical_alignment="center")
                if r1.checkbox(g, value=False, key=f"src-{g}", help="Using this asks which provider may receive the code"):
                    sel.add(g)
                if g in mine and r2.button("", key=f"pj-rm-{g}", icon=":material/delete:", type="tertiary",
                                           help="Take this project off the list. Your code is not touched."):
                    projects.remove(g)
                    with st.spinner("Re-indexing..."):
                        rebuild_index()
                    S.proj_msg = (True, f"Removed {g} from the list. Your code was not touched.")
                    st.rerun()
            if projects.lan_mode():
                st.caption("Adding a code project is switched off while the app is open to other devices (--lan).")
            else:
                S.setdefault("proj_round", 0)
                with st.expander("Add a code project", expanded=bool(S.get("proj_msg"))):
                    if S.get("proj_msg"):
                        done, msg = S.pop("proj_msg")
                        (st.success if done else st.error)(msg)
                    st.caption("The folder is only read, never changed, and nothing is sent anywhere when you add it. "
                               "Each question that would send code asks which provider may receive it.")
                    pj_path = st.text_input("Folder", placeholder="~/code/my_project", key=f"proj_path_{S.proj_round}")
                    pj_name = st.text_input("Name (optional)", placeholder="defaults to the folder name", key=f"proj_name_{S.proj_round}")
                    b1, b2 = st.columns(2)
                    if b1.button("Check folder", key="proj-check", use_container_width=True, disabled=not pj_path.strip()):
                        try:
                            pv = projects.preview(projects.check_path(pj_path), cfg)
                            st.info(f"{pv.readable} files would be read ({pv.size_bytes / 1e6:.1f} MB). {pv.skipped} other files are left out "
                                    "(images, secrets, large files, build folders). " + ("Examples: " + ", ".join(pv.examples) if pv.examples else
                                    "No readable code files were found."))
                        except ValueError as e:
                            st.error(str(e))
                    if b2.button("Add and re-index", key="proj-add", use_container_width=True, disabled=not pj_path.strip(), type="primary"):
                        try:
                            name = projects.add(pj_path, pj_name)
                            with st.spinner("Re-indexing (this can take a few minutes for a large project)..."):
                                rebuild_index()
                            S.proj_msg = (True, f"Added {name} and re-indexed. Tick it in the list to use it.")
                            S.kb_open = name                       # a deep read is the default: it opens next, and asks which provider first
                            S.proj_round += 1
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
            if S.get("kb_open"):
                knowledge_dialog(S.pop("kb_open"), False)
            depth = st.radio("Code depth", ["Retrieve", "Whole project", "Explore"], horizontal=True, key="code_depth", label_visibility="collapsed",
                             help="Retrieve sends only the best-matching pieces. Whole project sends every readable file of the ticked project in one request. "
                                  "Explore lets Claude open files itself in a read-only copy, through your Claude login (slower).")
            if depth == "Explore":
                st.caption("Claude reads a temporary read-only copy of the ticked project, using only Read, Grep and Glob. It takes minutes and counts toward your Claude plan limits.")
            if depth == "Whole project":
                ticked = [g for g in projs if g in sel]
                if ticked:
                    wp = projectctx.collect(cfg, ticked)
                    st.caption(f"About {wp.tokens:,} tokens in {len(wp.files)} files would be sent. You choose the provider first.")
                else:
                    st.caption("Tick a project above to use this.")
            if projs:
                with st.expander("Project knowledge"):
                    if S.get("kb_msg"):
                        done, msg = S.pop("kb_msg")
                        (st.success if done else st.error)(msg)
                    st.checkbox("Use it in answers", value=True, key="use_knowledge",
                                help="When a ticked project has knowledge built, the parts that match your question go with it.")
                    for g in projs:
                        kst = knowledge.status(cfg, g)
                        if kst.built:
                            st.caption(f"**{g}**: built {kst.built_at}, {kst.n_files} files, " + (f"{kst.n_changed} changed since" if kst.stale else "up to date")
                                       + f". {kst.verified} citations checked, {kst.unverified} unverified.")
                        else:
                            st.caption(f"**{g}**: not built")
                        b1, b2 = st.columns(2)
                        if b1.button("Update" if kst.built else "Build", key=f"kb-{g}", use_container_width=True, disabled=kst.built and not kst.stale):
                            knowledge_dialog(g, kst.built)
                        if b2.button("Delete", key=f"kd-{g}", use_container_width=True, disabled=not kst.built):
                            knowledge.delete(g)
                            st.rerun()
            st.caption("Folders named patient-data, and image and imaging files, are never read.")
            S.selected = sel
            m = index.meta
            st.caption(f"{len(sel)} selected · index {m['n_chunks']} passages ({m['backend']}) built {m['built_at']}")

            st.markdown('<span class="lbl">Add</span>', unsafe_allow_html=True)
            S.setdefault("add_round", 0)       # a new round gives empty URL and upload boxes after each successful add
            if S.get("added_msg"):
                st.success(S.pop("added_msg"))
            url = st.text_input("URL", placeholder="Paste a URL", label_visibility="collapsed", key=f"url_in_{S.add_round}")
            up = st.file_uploader("Upload", type=["pdf", "md", "txt"], label_visibility="collapsed", key=f"upload_{S.add_round}")
            if st.button("Add and re-index", use_container_width=True, disabled=not (url or up)):
                dest = settings.upload_dir()
                dest.mkdir(parents=True, exist_ok=True)
                try:
                    if up:
                        if len(up.getvalue()) > candidate.MAX_UPLOAD_BYTES:
                            raise ValueError("this file is larger than 20 MB")
                        target = settings.assert_writable(dest / candidate.safe_name(up.name))
                        if target.exists():
                            raise ValueError(f"{target.name} is already in your sources; rename the file or remove the old one first")
                        target.write_bytes(up.getvalue())
                    if url:
                        from bs4 import BeautifulSoup
                        body, final = webfetch.fetch(url)                      # public websites only, size and time limited
                        soup = BeautifulSoup(body, "html.parser")
                        for t in soup(["script", "style", "nav", "footer"]):
                            t.decompose()
                        name = re.sub(r"[^a-z0-9]+", "-", (urlparse(final).netloc + urlparse(final).path).lower()).strip("-")[:60] or "page"
                        title = html.escape(str(soup.title.string)) if soup.title and soup.title.string else final
                        (dest / f"{name}.md").write_text(f"# {title}\n\nSource: {final}\n\n{settings.redact(soup.get_text(chr(10), strip=True))}", encoding="utf-8")
                    with st.spinner("Re-indexing (about a minute)..."):
                        rebuild_index()
                    S.added_msg = "Added and re-indexed: " + ", ".join(x for x in (up.name if up else "", url) if x)
                    S.add_round += 1
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not add: {e}")

            st.markdown('<span class="lbl">Sessions</span>', unsafe_allow_html=True)
            ids = Session.list_ids(role=ROLE)
            pick = st.selectbox("Resume", ["(this chat)"] + ids, label_visibility="collapsed",
                                format_func=lambda i: i if i == "(this chat)" else Session.title(i))
            s1, s2, s3 = st.columns(3)
            if s1.button("Open", use_container_width=True, disabled=pick == "(this chat)"):
                S.session = Session.load(pick)
                S.views = [record_to_view(t) for t in S.session.turns]
                st.rerun()
            if s2.button("New", use_container_width=True, help="Start a new chat"):
                S.session, S.views, S.pending, S.consent_answer, S.evidence = Session(role=ROLE), [], None, None, None
                st.rerun()
            if s3.button("Delete", key="sess-del", use_container_width=True, disabled=pick == "(this chat)", help="Delete the chat chosen above"):
                S.confirm_delete = pick
            sid = S.get("confirm_delete")
            if sid:
                st.warning(f"Delete chat **{Session.title(sid)}**? This cannot be undone.")
                d1, d2 = st.columns(2)
                if d1.button("Delete", key="del-yes", use_container_width=True):
                    Session.delete(sid)
                    if S.session.id == sid:
                        S.session, S.views, S.pending, S.consent_answer, S.evidence = Session(role=ROLE), [], None, None, None
                    S.confirm_delete = None
                    st.rerun()
                if d2.button("Keep", key="del-no", use_container_width=True):
                    S.confirm_delete = None
                    st.rerun()
            if st.button("Re-index sources", use_container_width=True):
                with st.spinner("Re-indexing (about a minute)..."):
                    rebuild_index()
                st.rerun()

        with st.container(border=True):
            st.markdown("### Settings")
            ACCESS = {"auto": "Auto: login, API key only for web search", "login": "Login only (claude -p, no API key)", "api": "API key only (billed per use)"}
            st.selectbox("Claude access", list(ACCESS), index=list(ACCESS).index(cfg["models"].get("claude_access", "auto")),
                         format_func=ACCESS.get, key="claude_access")
            st.selectbox("Claude model", CLAUDE_MODELS, index=CLAUDE_MODELS.index(cfg["models"]["claude"]) if cfg["models"]["claude"] in CLAUDE_MODELS else 0, key="claude_model")
            st.selectbox("Gemini model", GEMINI_MODELS, index=GEMINI_MODELS.index(cfg["models"]["gemini"]) if cfg["models"]["gemini"] in GEMINI_MODELS else 0, key="gemini_model")
            st.selectbox("Ollama model", installed_models() or ["(none installed)"], key="ollama_model")
            st.checkbox("Use offline demo models (no keys, no network; answers are placeholders)", value=False, key="demo")
            for k, p in eng.providers.items():
                ok, why = p.available()
                st.caption(f"**{k}**: {p.label()} · {'ready' if ok else 'not ready. ' + why}")
            if not S.get("demo") and not eng.available() - {"ollama"}:
                st.info("No cloud model is ready. Make sure the `claude` command is installed and logged in, or set ANTHROPIC_API_KEY / GEMINI_API_KEY in .env, or tick the demo box to look around.")

# ---- right: evidence
with right:
    with st.container(key="side_right"):
        with st.container(border=True):
            st.markdown("### Evidence")
            show_evidence(chunk_by_id)
            st.markdown('<div class="flow"></div>', unsafe_allow_html=True)
            st.caption("Tags: local = your papers and notes · code = project files · web = a page the model searched · dashed = the model's own knowledge, unchecked.")

# ---- middle: conversation
with mid:
    with st.container(key="side_mid"):
        with st.container(border=True):
            with st.container(key="convbar"):          # stays in view while a long chat scrolls
                h1, h2, h3, h4 = st.columns([1.1, 2.4, 1.5, 1.3], vertical_alignment="center")
                h1.markdown("### Conversation")
                mode = h2.radio("Model", ["auto", "claude", "gemini", "compare", "ollama"], horizontal=True,
                                label_visibility="collapsed", key="mode")
                web = h3.toggle("Web search", value=S.get("web_on", cfg["web"]["default"]), key="web_w")
                memory = h4.toggle("Memory", value=S.get("memory_on", cfg["chat"]["memory"]), key="memory_w")
                S.web_on, S.memory_on = web, memory
            with st.container(key="answers"):
                for i, v in enumerate(S.views):
                    show_view(v, i, chunk_by_id)

            if not S.selected:
                st.info("No sources are ticked, so answers come from the model's own knowledge only. Tick sources on the left to search your files.")
            q = st.chat_input("Ask about your experience, this role, your papers or code. Say 'make an infographic of...' for a visual.")
            if q and not S.pending:
                S.pending = {"id": f"{time.time():.3f}", "q": q}
                S.consent_answer = None
                st.rerun()

            p = S.pending
            if p:
                st.markdown(f'<div class="q">{html.escape(p["q"])}</div>', unsafe_allow_html=True)
                try:
                    with st.spinner("Exploring the project (this can take a few minutes)..." if S.get("code_depth") == "Explore" else "Thinking..."):
                        res = eng.ask(p["q"], mode=mode, web=web, memory=memory, groups=set(S.selected or ()),
                                      code_mode={"Whole project": "whole", "Explore": "explore"}.get(S.get("code_depth"), "retrieve"),
                                      use_knowledge=S.get("use_knowledge", True),
                                      session=S.session, consent=ui_consent)
                except NeedConsent as nc:
                    consent_dialog(nc.req)      # nothing has been sent; the dialog decides what happens next
                else:
                    S.views.append(to_view(res))
                    S.pending, S.consent_answer = None, None   # the choice is never remembered
                    st.rerun()
