"""Configuration, paths and the safety rules that config cannot override."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR                             # the only place this tool may write
INDEX_DIR = TOOL_DIR / ".index"
SESSIONS_DIR = TOOL_DIR / "sessions"
OUTPUTS_DIR = TOOL_DIR / "outputs"
CACHE_DIR = TOOL_DIR / ".cache"
WORKSPACE_DIR = TOOL_DIR / "workspace"         # your profile and roles; private, never committed
KNOWLEDGE_DIR = TOOL_DIR / "knowledge"      # one knowledge document per project; private, never committed

# Never indexed, never entered, and not overridable by config or flags.
HARD_EXCLUDE_DIRS = {"patient-data", "patient_data", "patientdata"}

_PATIENT_RE = re.compile(r"^patient[-_ ]?data", re.I)      # also catches "patient data" and "patient-data-v2"

# File types that are never read, whatever the config says: images, imaging volumes, medical formats and raw arrays.
# (A NIfTI file's first bytes may be read for its header, but only in a project that asks for it; see projectctx.py.)
NEVER_READ_EXTS = {
    ".nii", ".gz", ".mgz", ".mgh", ".dcm", ".dicom", ".ima", ".nrrd", ".mha", ".mhd",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".svg", ".ico", ".heic",
    ".npy", ".npz", ".h5", ".hdf5", ".pt", ".pth", ".ckpt", ".onnx", ".pkl", ".pickle",
    ".img", ".hdr", ".mnc", ".pgm", ".ppm", ".jp2", ".avif", ".raw", ".mat", ".zarr", ".vtk", ".parquet",
}

# Skipped by default (config may add more, never remove).
DEFAULT_EXCLUDE_DIRS = {
    "node_modules", "__pycache__", "venv", "env", "dist", "build", "checkpoints",
    "data", "datasets", "outputs", "wandb", "runs", "logs", "site-packages",
}
SECRET_FILE_RE = re.compile(r"(^\.env)|secret|credential|id_rsa|\.pem$|\.key$|\.p12$|token", re.I)
_NAMES = r"(?:api[_-]?key|secret|token|passwd|password|access[_-]?key|client[_-]?secret|private[_-]?key|auth)"
SECRET_PATTERNS = [
    # quoted values:  password = "hunter2hunter2"
    (re.compile(r"""(?i)(\b\w{0,40}""" + _NAMES + r"""\w{0,40}["']?\s*[:=]\s*["'])([^"'\s]{8,})(["'])"""), r"\1REDACTED\3"),
    # unquoted values containing a digit:  password: abc123def456   (plain identifiers like get_token are kept)
    (re.compile(r"""(?i)(\b\w{0,40}""" + _NAMES + r"""\w{0,40}\s*[:=]\s*)(?=[^\s"',;#]*\d)([A-Za-z0-9_\-/+=.]{10,})"""), r"\1REDACTED"),
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]{0,30}://[^/\s:@]+:)([^@\s/]+)(@)"), r"\1REDACTED\3"),   # user:pass@host
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "REDACTED"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "REDACTED"),  # JWT
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "REDACTED"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "REDACTED"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "REDACTED"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "REDACTED PRIVATE KEY"),
]


def setup_env() -> None:
    """Keep model caches inside the project instead of ~/.cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(CACHE_DIR / "hf"))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(CACHE_DIR / "st"))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    load_dotenv()


KEY_NAMES = {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "INTERVIEW_PREP_PASSCODE"}


def load_dotenv(path: Path | None = None) -> None:
    """Read the API keys from .env (written by the user, never by the tool).

    Only the key names above (and the optional phone passcode) are accepted, and a variable already set in the terminal wins.
    """
    p = path or TOOL_DIR / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if name in KEY_NAMES and value:
            os.environ.setdefault(name, value)


LOCAL_CONFIG = TOOL_DIR / "config.local.yaml"      # your own paths; never committed


def _merge(base: dict, over: dict) -> dict:
    """Nested dicts merge; everything else (lists, numbers, strings) is replaced by the override."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def load_config(path: Path | None = None) -> dict:
    """config.yaml, with config.local.yaml (if present) laid over it."""
    with open(path or TOOL_DIR / "config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if path is None and LOCAL_CONFIG.is_file():
        with open(LOCAL_CONFIG, "r", encoding="utf-8") as f:
            cfg = _merge(cfg, yaml.safe_load(f) or {})
    return cfg


def group_path(cfg: dict, group: str) -> Path | None:
    """The file behind a document source name such as 'library/notes.md', or None."""
    for src in cfg.get("docs", []):
        base = resolve(src["path"])
        if group.startswith(base.name + "/"):
            p = base / group[len(base.name) + 1:]
            if p.is_file() and base.resolve() in p.resolve().parents and not in_hard_excluded(p):
                return p
    return None


def upload_dir() -> Path:
    """Where files added through the app land. It is a document source in the default config."""
    return assert_writable(ROOT / "library" / "added")


def resolve(p: str) -> Path:
    q = Path(p).expanduser()
    return q if q.is_absolute() else (ROOT / q)


def never_read(path) -> bool:
    return Path(path).suffix.lower() in NEVER_READ_EXTS


def in_hard_excluded(path: Path) -> bool:
    return any(part.lower() in HARD_EXCLUDE_DIRS or _PATIENT_RE.search(part) for part in Path(path).parts)


def exclude_dirs(cfg: dict) -> set[str]:
    return DEFAULT_EXCLUDE_DIRS | {str(x).lower() for x in cfg.get("extra_excludes", [])}


def assert_writable(path: Path) -> Path:
    """Refuse writes outside the tool folder."""
    p = Path(path).resolve()
    if ROOT.resolve() not in p.parents and p != ROOT.resolve():
        raise PermissionError(f"Refusing to write outside {ROOT}: {p}")
    return p


def redact(text: str) -> str:
    for rx, rep in SECRET_PATTERNS:
        text = rx.sub(rep, text)
    return text


REMOVED_FILE = TOOL_DIR / "removed_sources.json"


def removed_sources() -> set[str]:
    """Sources the user took off the desk. They stay on disk but are never searched."""
    import json
    try:
        return set(json.loads(REMOVED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def set_removed_sources(names: set[str]) -> None:
    import json
    assert_writable(REMOVED_FILE).write_text(json.dumps(sorted(names), indent=1), encoding="utf-8")
