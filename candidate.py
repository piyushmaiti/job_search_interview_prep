"""Your profile and your roles: what the interview prep tool knows about you.

One profile is shared by every role. It is built once from a CV or resume, an optional "about you" text and optional
publications, and kept as a Markdown file you can read and correct. Each role (one job you are preparing for) has its own
folder with the pasted job description, your intent for that application and the CV you send there.

Everything lives in workspace/ (private, never committed):
  workspace/profile/   cv.pdf|md|txt, about.md, publications/*.pdf
  workspace/roles/<role>/   role.json, job_description.md, intent.md, cv.pdf|md|txt
Both folders are document sources in config.yaml, so everything in them is searchable and citable like any other source.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import settings

KINDS = ("Industry", "Academic")
CV_EXTS = (".pdf", ".docx", ".md", ".txt")
PROFILE_PINNED = ["about.md", "cv.*"]
ROLE_PINNED = ["job_description.md", "intent.md"]
MAX_UPLOAD_BYTES = 20_000_000
MAX_DOCX_XML = 10_000_000          # unpacked text of a .docx; a real CV is far smaller

ROLE_PROMPT = (
    "preparing for job interviews. The documents about the user (about.md, cv) and the job description of the role "
    "being prepared are provided as sources. Answer about the user only from those documents and cite them; never invent "
    "experience, employers, dates, numbers or skills. If the documents do not say it, say it is not in their documents.")



# ---------------------------------------------------------------- paths
def workspace() -> Path:
    return settings.WORKSPACE_DIR


def profile_dir() -> Path:
    return workspace() / "profile"


def roles_dir() -> Path:
    return workspace() / "roles"


def _write(path: Path, data: bytes | str) -> Path:
    settings.assert_writable(path).parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def safe_name(name: str) -> str:
    """A plain file name: no folders, no odd characters."""
    stem = re.sub(r"[^A-Za-z0-9._\- ]+", "_", Path(name).name).strip(" .") or "file"
    return stem[:120]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "role"


# ---------------------------------------------------------------- reading uploads
def docx_text(data: bytes) -> str:
    """Plain text of a .docx file, without any extra package (a .docx is a zip of XML)."""
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML:
                raise ValueError("this .docx unpacks to more text than a CV should hold")
            with z.open(info) as f:
                xml = f.read(MAX_DOCX_XML + 1).decode("utf-8", errors="replace")      # never more than the cap, whatever the header says
            if len(xml) > MAX_DOCX_XML:
                raise ValueError("this .docx unpacks to more text than a CV should hold")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError("this is not a readable .docx file") from e
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_text(path: Path, max_chars: int = 200_000) -> str:
    from pypdf import PdfReader
    out, n = [], 0
    for page in PdfReader(str(path)).pages:
        t = page.extract_text() or ""
        out.append(t)
        n += len(t)
        if n > max_chars:
            break
    return "\n\n".join(out)[:max_chars]


def read_text(path: Path, max_chars: int = 200_000) -> str:
    if path.suffix.lower() == ".pdf":
        return pdf_text(path, max_chars)
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def store_document(folder: Path, base: str, filename: str, data: bytes) -> Path:
    """Save an uploaded CV-like document as <base>.<ext>. PDF, Markdown and text are kept as they are (and indexed);
    a .docx is saved as its text, because only those three types are indexed. Any older copy under the same base is removed."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("this file is larger than 20 MB")
    ext = Path(filename).suffix.lower()
    if ext not in CV_EXTS:
        raise ValueError(f"unsupported file type {ext or '(none)'}; use PDF, DOCX, Markdown or text")
    if ext == ".docx":
        name, payload = f"{base}.md", docx_text(data)
    elif ext == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("this file does not look like a PDF")
    else:
        name, payload = f"{base}{ext}", data
    for old in folder.glob(f"{base}.*") if folder.exists() else []:
        settings.assert_writable(old).unlink(missing_ok=True)
    return _write(folder / name, payload)


def find_document(folder: Path, base: str) -> Path | None:
    for ext in (".pdf", ".md", ".txt"):
        p = folder / f"{base}{ext}"
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------- profile
def save_cv(filename: str, data: bytes) -> Path:
    return store_document(profile_dir(), "cv", filename, data)


def cv_path() -> Path | None:
    return find_document(profile_dir(), "cv")


def has_profile_cv() -> bool:
    return cv_path() is not None


def save_about(text: str) -> None:
    if text.strip():
        _write(profile_dir() / "about.md", f"# About me\n\n{text.strip()}\n")
    else:
        settings.assert_writable(profile_dir() / "about.md").unlink(missing_ok=True)


def read_about() -> str:
    p = profile_dir() / "about.md"
    if not p.is_file():
        return ""
    return re.sub(r"\A# About me\n\n", "", p.read_text(encoding="utf-8")).strip()


def save_publication(filename: str, data: bytes) -> Path:
    if Path(filename).suffix.lower() not in (".pdf", ".md", ".txt"):
        raise ValueError("publications can be PDF, Markdown or text")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("this file is larger than 20 MB")
    if Path(filename).suffix.lower() == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("this file does not look like a PDF")
    return _write(profile_dir() / "publications" / safe_name(filename), data)


def list_publications() -> list[str]:
    d = profile_dir() / "publications"
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def delete_publication(name: str) -> None:
    settings.assert_writable(profile_dir() / "publications" / safe_name(name)).unlink(missing_ok=True)


# ---------------------------------------------------------------- roles
@dataclass
class Role:
    slug: str
    name: str
    company: str = ""
    kind: str = "Industry"
    created: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}, {self.company}" if self.company else self.name

    @property
    def folder(self) -> Path:
        return roles_dir() / self.slug

    @property
    def prefix(self) -> str:
        """Start of the source names of this role's files in the index."""
        return f"roles/{self.slug}/"

    def job_description(self) -> str:
        return _read_or_empty(self.folder / "job_description.md")

    def intent(self) -> str:
        return _read_or_empty(self.folder / "intent.md")

    def cv(self) -> Path | None:
        return find_document(self.folder, "cv")


def _read_or_empty(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _body(text: str) -> str:
    """The user's own text, without the heading this tool adds when it saves the file."""
    return re.sub(r"\A# [^\n]*\n\n", "", text).strip()


def job_description_text(role: Role) -> str:
    return _body(role.job_description())


def intent_text(role: Role) -> str:
    return _body(role.intent())


def get_role(slug: str) -> Role | None:
    if not re.fullmatch(r"[a-z0-9\-]+", slug or ""):
        return None
    try:
        raw = json.loads((roles_dir() / slug / "role.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Role(slug, raw.get("name", slug), raw.get("company", ""), raw.get("kind", "Industry"), raw.get("created", ""))


def list_roles() -> list[Role]:
    d = roles_dir()
    if not d.is_dir():
        return []
    roles = [r for r in (get_role(p.name) for p in d.iterdir() if p.is_dir()) if r]
    return sorted(roles, key=lambda r: r.created)


def _save_texts(role: Role, jd: str, intent: str) -> None:
    _write(role.folder / "job_description.md", f"# Job description: {role.label}\n\n{jd.strip()}\n")
    _write(role.folder / "intent.md", f"# My intent for this application: {role.label}\n\n{intent.strip() or 'Not stated.'}\n")


def create_role(name: str, company: str, kind: str, jd: str, intent: str, cv_name: str = "", cv_data: bytes | None = None,
                use_main_cv: bool = False) -> Role:
    name, company = " ".join(name.split()), " ".join(company.split())
    if not name:
        raise ValueError("Give the role a title.")
    if kind not in KINDS:
        raise ValueError("Choose Industry or Academic.")
    if not jd.strip():
        raise ValueError("Paste the job description.")
    if not use_main_cv and not cv_data:
        raise ValueError("Upload the CV or resume you are sending for this role, or tick 'Use my main CV'.")
    if use_main_cv and not cv_path():
        raise ValueError("There is no main CV yet. Add one on the Profile page, or upload a CV for this role.")
    base = slugify(f"{name} {company}")
    slug, n = base, 2
    while (roles_dir() / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    role = Role(slug, name, company, kind, datetime.now().isoformat(timespec="milliseconds"))
    try:
        if cv_data:
            store_document(role.folder, "cv", cv_name, cv_data)
        else:
            src = cv_path()
            _write(role.folder / f"cv{src.suffix}", src.read_bytes())
        _save_texts(role, jd, intent)
        _write(role.folder / "role.json", json.dumps({"name": name, "company": company, "kind": kind, "created": role.created}, indent=1))
    except Exception:
        shutil.rmtree(settings.assert_writable(role.folder), ignore_errors=True)
        shutil.rmtree(settings.assert_writable(workspace() / "prep" / slug), ignore_errors=True)       # its prep results and mock interviews
        raise
    return role


def update_role_texts(role: Role, jd: str, intent: str) -> None:
    if not jd.strip():
        raise ValueError("The job description cannot be empty.")
    _save_texts(role, jd, intent)


def replace_role_cv(role: Role, filename: str, data: bytes) -> None:
    store_document(role.folder, "cv", filename, data)


def delete_role(slug: str) -> None:
    role = get_role(slug)
    if role:
        shutil.rmtree(settings.assert_writable(role.folder), ignore_errors=True)
        shutil.rmtree(settings.assert_writable(workspace() / "prep" / slug), ignore_errors=True)       # its prep results and mock interviews


# ---------------------------------------------------------------- how the roles reach the search
# Files that earlier versions of this tool wrote into workspace/profile/. They are no longer used. They stay on disk until the
# person deletes them on the Profile page, and meanwhile they are hidden from the sources so they are never searched or sent.
LEGACY_FILES = ("profile.md", "profile.json", "facts.md", "facts.json", "about_clean.json", "about_original.json")


def legacy_files() -> list[Path]:
    return [p for p in (profile_dir() / n for n in LEGACY_FILES) if p.is_file()]


def delete_legacy_files() -> int:
    n = 0
    for p in legacy_files():
        settings.assert_writable(p).unlink(missing_ok=True)
        n += 1
    return n


def visible(group: str, role_slug: str | None) -> bool:
    """A source belongs in the current view unless it is another role's file or a leftover file from an earlier version."""
    if group in ("profile/profile.md", "profile/facts.md"):
        return False
    if not group.startswith("roles/"):
        return True
    return bool(role_slug) and group.startswith(f"roles/{role_slug}/")


def is_workspace(group: str) -> bool:
    return group.startswith(("profile/", "roles/"))


def configured(cfg: dict, role: Role | None) -> dict:
    """A copy of the config for one role: its profile and role documents are always sent, and the model is told what the tool is for."""
    out = dict(cfg)
    r = dict(cfg.get("retrieval", {}))
    pinned = list(PROFILE_PINNED) + (list(ROLE_PINNED) if role else [])
    r["pinned_docs"] = list(r.get("pinned_docs") or []) + pinned
    r["pinned_when"] = list(r.get("pinned_when") or []) + ["."]          # every question
    out["retrieval"] = r
    about = ROLE_PROMPT
    if role:
        about += f" The role being prepared: {role.label} ({role.kind.lower()} role)."
    out["assistant_role"] = (cfg.get("assistant_role") or "").strip() or about
    return out


def source_label(group: str) -> str:
    """A readable name for one of your own files in the list of sources."""
    parts = group.split("/")
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    if group.startswith("profile/publications/"):
        return f"Publication: {stem}"
    if group.startswith("profile/"):
        return {"about": "About me", "cv": "My main CV"}.get(stem, name)
    if group.startswith("roles/"):
        return {"job_description": "Job description", "intent": "My intent", "cv": "CV for this role"}.get(stem, name)
    return name


# ---------------------------------------------------------------- is everything in the index?
def _words(text: str) -> list[str]:
    import unicodedata
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text or "").casefold())


def index_report(index) -> list[dict]:
    """For the CV and About you: how many characters there are, how many index pieces they became, what share of the text is in
    those pieces, and anything that could mean something is missing. Counted from the real index, not assumed."""
    rows = []
    for label, path in (("CV", cv_path()), ("About you", profile_dir() / "about.md")):
        if not path or not path.is_file():
            continue
        row = {"name": label, "file": path.name, "chars": 0, "pieces": 0, "covered": 0.0, "notes": []}
        try:
            text = read_text(path)
        except Exception as e:
            row["notes"].append(f"could not be read: {e}")
            rows.append(row)
            continue
        row["chars"] = len(text)
        if path.suffix.lower() == ".pdf" and len(text.strip()) < 200:
            row["notes"].append("has almost no text. A scanned PDF has no text layer, so nothing can be indexed from it. Upload a text version.")
        if len(text) >= 200_000:
            row["notes"].append("is very long, so only its first 200,000 characters are read.")
        if settings.redact(text) != text:
            row["notes"].append("has places that look like a password or key. They are hidden in the index.")
        chunks = [c for c in index.chunks if c.group == f"profile/{path.name}"]
        row["pieces"] = len(chunks)
        if not chunks:
            row["notes"].append("is not in the search index yet. Press Re-index sources.")
        else:
            from collections import Counter
            have = Counter()
            for c in chunks:                                   # a chunk's heading is kept beside its text, so count both
                have.update(_words(f"{c.section or ''} {c.text}"))
            src = Counter(_words(text))
            total = sum(src.values())
            row["covered"] = (sum((src & have).values()) / total) if total else 1.0     # each word of the text, counted as often as it occurs
            if row["covered"] < 0.98:
                row["notes"].append(f"only {row['covered']:.0%} of its text was found in the index pieces. Re-index, and tell me if it stays low.")
        rows.append(row)
    return rows
