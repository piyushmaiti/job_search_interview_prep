"""Whole-project context: every readable file of a project, read fresh and sent in one request.

Files are found with the same walker and rules as indexing (excluded folders, secret files, size limit, never any
image or imaging file), then low-value files are skipped, secrets are redacted, and the result is trimmed to fit the
chosen model. A project can also ask for a short text summary of its NIfTI files (names, sizes and header fields only;
the voxel data is never read).
"""
from __future__ import annotations

import fnmatch
import gzip
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import settings
from ingest import Chunk, _cid, iter_files, walk_files

DEFAULT_SKIP = ["*.tex", "*.log", "*requirements*.txt", "*lock*", "*train_log*"]
DEFAULT_CAPS = {"claude": 150_000, "gemini": 700_000, "openai": 400_000}
DATA_FILES = "(data files)"
SUMMARY_CHARS = 6000

NIFTI_DTYPES = {2: "uint8", 4: "int16", 8: "int32", 16: "float32", 32: "complex64", 64: "float64", 256: "int8",
                512: "uint16", 768: "uint32", 1024: "int64", 1280: "uint64", 1536: "float128"}
NIFTI_UNITS = {1: "m", 2: "mm", 3: "um"}


def estimate_tokens(chars: int) -> int:
    return (chars + 3) // 4


# ---------------------------------------------------------------- NIfTI headers
def read_nifti_header(path: Path) -> dict | None:
    """Shape, voxel size, unit and data type from the first bytes of a NIfTI-1 or NIfTI-2 file. Nothing else is read."""
    try:
        with (gzip.open(path, "rb") if path.name.lower().endswith(".gz") else open(path, "rb")) as f:
            head = f.read(540)
    except (OSError, EOFError):
        return None
    if len(head) < 348:
        return None
    for e in ("<", ">"):
        size = struct.unpack(e + "i", head[:4])[0]
        if size == 348:
            dim = struct.unpack(e + "8h", head[40:56])
            dtype = struct.unpack(e + "h", head[70:72])[0]
            pix = struct.unpack(e + "8f", head[76:108])
            unit = head[123] & 7
        elif size == 540 and len(head) >= 540:
            dim = struct.unpack(e + "8q", head[16:80])
            dtype = struct.unpack(e + "h", head[12:14])[0]
            pix = struct.unpack(e + "8d", head[104:168])
            unit = head[500] & 7
        else:
            continue
        ndim = dim[0]
        if not 1 <= ndim <= 7:
            return None
        return {"shape": tuple(dim[1:1 + ndim]), "voxel": tuple(round(float(v), 4) for v in pix[1:1 + ndim]),
                "unit": NIFTI_UNITS.get(unit, ""), "dtype": NIFTI_DTYPES.get(dtype, f"code {dtype}")}
    return None


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def data_files_summary(base: Path, cfg: dict) -> str:
    """Names and sizes of the imaging and image files in a project, and the header fields of its NIfTI files."""
    nifti: list[str] = []
    other: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for p in walk_files(base, cfg):
        name = p.name.lower()
        if settings.SECRET_FILE_RE.search(p.name):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        rel = p.relative_to(base).as_posix()
        if name.endswith((".nii", ".nii.gz")):
            h = read_nifti_header(p)
            if h:
                shape = "x".join(str(v) for v in h["shape"])
                vox = "x".join(f"{v:g}" for v in h["voxel"])
                nifti.append(f"- {rel} ({_size(size)}): shape {shape}, voxel {vox} {h['unit']}, {h['dtype']}".replace("  ", " "))
            else:
                nifti.append(f"- {rel} ({_size(size)}): header not readable")
        elif settings.never_read(p) and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".svg"):
            key = (str(Path(rel).parent), p.suffix.lower())
            other[key][0] += 1
            other[key][1] += size
    if not nifti and not other:
        return ""
    out = ["DATA FILES (names, sizes and NIfTI header fields only; the files themselves are not read)"]
    if nifti:
        out.append("NIfTI volumes:")
        out += nifti[:40]
        if len(nifti) > 40:
            out.append(f"... and {len(nifti) - 40} more")
    if other:
        out.append("Image files:")
        for (folder, ext), (n, total) in sorted(other.items())[:40]:
            out.append(f"- {folder}/: {n} {ext} files, {_size(total)} in total")
    text = "\n".join(out)
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS - 30] + "\n... (cut to keep it short)"


# ---------------------------------------------------------------- collecting
@dataclass
class ProjectFile:
    project: str
    rel: str
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(len(self.text))

    @property
    def source(self) -> str:
        return f"{self.project}/{self.rel}"


@dataclass
class WholeProject:
    files: list[ProjectFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)      # left out as low-value
    omitted: list[str] = field(default_factory=list)      # left out to fit the model

    @property
    def tokens(self) -> int:
        return sum(f.tokens for f in self.files)

    def trim_to(self, cap_tokens: int) -> list[str]:
        """Drop the largest files until the rest fits. The data-files summary is kept. Returns what was dropped."""
        dropped: list[str] = []
        while self.tokens > cap_tokens:
            big = max((f for f in self.files if f.rel != DATA_FILES), key=lambda f: f.tokens, default=None)
            if big is None:
                break
            self.files.remove(big)
            dropped.append(big.source)
        self.omitted += dropped
        return dropped

    def chunks(self) -> list[Chunk]:
        out = []
        for f in self.files:
            n = f.text.count("\n") + 1
            out.append(Chunk(_cid(f.project, f.rel, "whole"), "code", f.project, f.source, f.text,
                             section="data files" if f.rel == DATA_FILES else None, line_start=1, line_end=n))
        return out


def project_names(cfg: dict) -> list[str]:
    return [p["name"] for p in cfg.get("projects", [])]


_READ_CACHE: dict = {}


def _read_redacted(p: Path) -> str:
    """The redacted text of a file; an unchanged file (same size and modified time) is not read or scanned again."""
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return settings.redact(p.read_text(encoding="utf-8", errors="replace"))
    if key not in _READ_CACHE:
        _READ_CACHE[key] = settings.redact(p.read_text(encoding="utf-8", errors="replace"))
    return _READ_CACHE[key]


def collect(cfg: dict, names: list[str]) -> WholeProject:
    wp = cfg.get("whole_project", {})
    skip = [g.lower() for g in wp.get("skip_globs", DEFAULT_SKIP)]
    exts = {e.lower() for e in cfg["project_exts"]}
    out = WholeProject()
    for proj in cfg.get("projects", []):
        if proj["name"] not in names:
            continue
        base = settings.resolve(proj["path"])
        if not base.exists():
            continue
        files: list[ProjectFile] = []
        for p in iter_files(base, exts, cfg):
            rel = p.relative_to(base).as_posix()
            if any(fnmatch.fnmatch(p.name.lower(), g) or fnmatch.fnmatch(rel.lower(), g) for g in skip):
                out.skipped.append(f"{proj['name']}/{rel}")
                continue
            files.append(ProjectFile(proj["name"], rel, _read_redacted(p)))
        files.sort(key=lambda f: (0 if f.rel.lower().endswith(".md") and "/" not in f.rel else 1, f.rel))
        if proj.get("nifti_headers"):
            summary = data_files_summary(base, cfg)
            if summary:
                files.append(ProjectFile(proj["name"], DATA_FILES, summary))
        out.files += files
    return out


def caps(cfg: dict) -> dict[str, int]:
    return {**DEFAULT_CAPS, **{k: int(v) for k, v in cfg.get("whole_project", {}).get("max_tokens", {}).items()}}
