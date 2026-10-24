"""Environment diagnostics for the `doctor` command (spec 15).

The report holds facts about this machine only, never user or message data. Every check
returns one Check and never raises. Native modules are imported in a child process, so a
DLL that an application-control policy blocks cannot take the report down with it.
"""

import base64
import ctypes
import dataclasses
import functools
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import sysconfig
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple

from emailsupersearch import paths

Status = Literal["ok", "warn", "fail", "skip"]

MAX_SITE_PACKAGES_PATH = 130
MIN_RAM_GB = 8
MIN_FREE_DISK_GB = 10
CRT_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140_1.dll", "vcomp140.dll")
STORE_PYTHON_MARKERS = ("windowsapps", "pythonsoftwarefoundation")
REQUIRED_MODULES = (
    "onnxruntime",
    "sqlite_vec",
    "tokenizers",
    "pypdfium2",
    "PIL",
    "watchfiles",
    *(("winrt.windows.media.ocr",) if sys.platform == "win32" else ()),
)
OPTIONAL_MODULES = ("rapidocr", "cv2")
PROBE_TIMEOUT_S = 60
SIGNER_TIMEOUT_S = 60
MSG_HANDLER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.msg\UserChoice"
GB = 1024**3


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    remedy: str = ""


@dataclass
class DoctorReport:
    checks: list[Check]
    degraded: list[str]
    generated_at: int


class _Result(NamedTuple):
    """What a check body returns; `_check` adds the check's name."""

    status: Status
    detail: str
    remedy: str = ""


def _check(*, windows_only: bool = False) -> Callable[[Callable[..., _Result]], Callable[..., Check]]:
    """Turn a body returning a _Result into a check named after the function that never raises."""

    def decorate(body: Callable[..., _Result]) -> Callable[..., Check]:
        name = body.__name__

        @functools.wraps(body)
        def run(*args: Any, **kwargs: Any) -> Check:
            if windows_only and sys.platform != "win32":
                return Check(name, "skip", "Windows only")
            try:
                return Check(name, *body(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - a check must report its failure, never raise
                return Check(name, "fail", f"{type(exc).__name__}: {exc}")

        return run

    return decorate


@_check()
def python_build() -> _Result:
    v = sys.version_info
    detail = f"Python {v.major}.{v.minor}.{v.micro} ({struct.calcsize('P') * 8}-bit), prefix {sys.prefix}"
    if sys.base_prefix != sys.prefix:
        detail += f", base {sys.base_prefix}"
    if any(marker in prefix.lower() for prefix in (sys.prefix, sys.base_prefix) for marker in STORE_PYTHON_MARKERS):
        return _Result(
            "warn",
            f"{detail}; Microsoft Store Python detected",
            "Store Python redirects writes under AppData to a private folder; prefer the python.org installer for the current user",
        )
    return _Result("ok", detail)


@_check()
def path_headroom() -> _Result:
    length = len(str(sysconfig.get_paths()["purelib"]))
    detail = f"site-packages path is {length} characters (warning above {MAX_SITE_PACKAGES_PATH})"
    if length > MAX_SITE_PACKAGES_PATH:
        return _Result("warn", detail, "create the virtual environment in a shorter folder so long package file names stay under the 260-character Windows limit")
    return _Result("ok", detail)


@_check(windows_only=True)
def crt_dlls() -> _Result:
    present, missing = [], []
    for name in CRT_DLLS:
        try:
            ctypes.WinDLL(name)
            present.append(name)
        except OSError:
            missing.append(name)
    detail = f"present: {', '.join(present) or 'none'}; missing: {', '.join(missing) or 'none'}"
    if missing:
        return _Result("fail", detail, "pip install msvc-runtime")
    return _Result("ok", detail)


@_check(windows_only=True)
def vulkan_loader() -> _Result:
    try:
        loader = ctypes.WinDLL("vulkan-1.dll")
    except OSError:
        return _Result("warn", "Vulkan loader not found; the LLM engine will use the CPU build")
    try:
        enumerate_version = loader.vkEnumerateInstanceVersion
    except AttributeError:
        return _Result("ok", "Vulkan 1.0 loader (vkEnumerateInstanceVersion is not exported)")
    enumerate_version.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    enumerate_version.restype = ctypes.c_int32
    version = ctypes.c_uint32(0)
    code = enumerate_version(ctypes.byref(version))
    if code != 0:
        return _Result("warn", f"Vulkan loader present but vkEnumerateInstanceVersion returned VkResult {code}")
    v = version.value
    return _Result("ok", f"Vulkan loader present, instance version {v >> 22}.{(v >> 12) & 0x3FF}.{v & 0xFFF}")


class ProbeResult(NamedTuple):
    """Outcome of the child-process import probe: per-module entries, or why it could not run."""

    modules: dict[str, dict[str, Any]]
    error: str = ""


# Runs in a child interpreter: imports each module in turn and prints one JSON object
# {module: {ok, error, missing, file, binaries}}. Binaries are the .pyd/.dll files under the
# package directory (recursive, at most 40), found even when the import fails, so a blocked
# DLL still gets its signer reported. The pdfium DLL lives in a sibling package, and winrt
# keeps its extension in the top-level package, hence EXTRA_PACKAGES and the top-level scan.
_PROBE_SCRIPT = r"""
import contextlib, importlib, importlib.util, io, json, sys
from pathlib import Path

MAX_BINARIES = 40
EXTRA_PACKAGES = {"pypdfium2": ["pypdfium2_raw"]}


def binaries_for(name):
    top = name.split(".")[0]
    found = []
    for candidate in dict.fromkeys([name, top, *EXTRA_PACKAGES.get(top, [])]):
        try:
            spec = importlib.util.find_spec(candidate)
        except Exception:
            continue
        if spec is None:
            continue
        roots = list(spec.submodule_search_locations or [])
        if not roots and spec.origin and spec.origin.lower().endswith((".pyd", ".dll")):
            roots = [spec.origin]
        for root in map(Path, roots):
            for path in [root] if root.is_file() else root.rglob("*"):
                if path.suffix.lower() in (".pyd", ".dll") and str(path) not in found:
                    found.append(str(path))
    return sorted(found)[:MAX_BINARIES]


result = {}
for name in json.loads(sys.argv[1]):
    entry = {"ok": False, "error": None, "missing": False, "file": None, "binaries": []}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            module = importlib.import_module(name)
        entry["ok"] = True
        entry["file"] = getattr(module, "__file__", None)
    except BaseException as exc:
        entry["error"] = type(exc).__name__ + ": " + str(exc)
        entry["missing"] = isinstance(exc, ModuleNotFoundError) and (exc.name or "").split(".")[0] == name.split(".")[0]
    entry["binaries"] = binaries_for(name)
    result[name] = entry
print(json.dumps(result))
"""


def _probe_modules() -> ProbeResult:
    names = [*REQUIRED_MODULES, *OPTIONAL_MODULES]
    command = [sys.executable, "-c", _PROBE_SCRIPT, json.dumps(names)]
    try:
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult({}, f"import probe timed out after {PROBE_TIMEOUT_S} s")
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeResult({}, f"import probe could not run: {exc}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        modules = json.loads(lines[-1])
    except (IndexError, ValueError):
        modules = None
    if not isinstance(modules, dict):
        stderr = proc.stderr.strip().splitlines()
        return ProbeResult({}, f"import probe crashed (exit code {proc.returncode}): {stderr[-1] if stderr else 'no output'}")
    return ProbeResult(modules)


class _SignerUnavailable(Exception):
    pass


_CN = re.compile(r'CN=(?:"((?:[^"]|"")*)"|([^,]*))')


def _signer_name(subject: str | None) -> str:
    if not subject:
        return "unknown signer"
    match = _CN.search(subject)
    name = ((match.group(1) or "").replace('""', '"') or match.group(2) or "").strip() if match else ""
    return name or subject


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _signer_statuses(binaries: list[str]) -> dict[str, str]:
    """Map each binary to `signed:<signer>` or `unsigned:<status>` with one PowerShell call."""
    quoted = ",".join("'" + path.replace("'", "''") + "'" for path in binaries)
    # The result travels as base64 of UTF-8 JSON: a redirected PowerShell 5.1 writes in the OEM
    # code page, which would mangle accented folder names and signer names.
    script = (
        "$rows = @(Get-AuthenticodeSignature -LiteralPath " + quoted + " | Select-Object Path, "
        "@{n='Status';e={[string]$_.Status}}, @{n='Subject';e={$_.SignerCertificate.Subject}}); "
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -Compress -InputObject $rows)))"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=SIGNER_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise _SignerUnavailable(f"PowerShell timed out after {SIGNER_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise _SignerUnavailable(f"PowerShell could not start: {exc}") from exc
    try:
        rows = json.loads(base64.b64decode(proc.stdout.strip(), validate=True).decode("utf-8"))
    except ValueError as exc:
        raise _SignerUnavailable(f"PowerShell exit code {proc.returncode}, no result") from exc
    if isinstance(rows, dict):
        rows = [rows]
    statuses = {}
    for row in rows:
        if row.get("Status") == "Valid":
            statuses[_path_key(row["Path"])] = f"signed:{_signer_name(row.get('Subject'))}"
        else:
            statuses[_path_key(row["Path"])] = f"unsigned:{row.get('Status')}"
    return statuses


def _describe_module(name: str, entry: dict[str, Any], signers: dict[str, str]) -> str:
    if entry["ok"]:
        head = f"{name}: loaded"
    elif entry["missing"] and name in OPTIONAL_MODULES:
        head = f"{name}: skipped (not installed)"
    else:
        head = f"{name}: not loaded ({entry['error']})"
    files = [f"{Path(p).name} {signers.get(_path_key(p), 'signer unknown')}" for p in entry["binaries"]]
    return f"{head}; {', '.join(files)}" if files else head


@_check()
def native_imports(probe: ProbeResult | None = None) -> _Result:
    if probe is None:
        probe = _probe_modules()
    if probe.error:
        return _Result("fail", probe.error)
    modules = probe.modules
    binaries = sorted({p for entry in modules.values() for p in entry["binaries"]})
    signers: dict[str, str] = {}
    signer_note = ""
    if binaries:
        try:
            signers = _signer_statuses(binaries)
        except _SignerUnavailable as exc:
            signer_note = f"signer check unavailable ({exc})"
    names = [name for name in [*REQUIRED_MODULES, *OPTIONAL_MODULES] if name in modules]
    loaded = sum(1 for name in names if modules[name]["ok"])
    skipped = sum(1 for name in names if not modules[name]["ok"] and modules[name]["missing"] and name in OPTIONAL_MODULES)
    lines = [f"{loaded} of {len(names)} modules loaded, {skipped} optional not installed"]
    lines += [_describe_module(name, modules[name], signers) for name in names]
    if signer_note:
        lines.append(signer_note)
    required_failed = any(not modules.get(name, {}).get("ok") for name in REQUIRED_MODULES)
    optional_broken = any(
        not entry["ok"] and not entry["missing"] for name, entry in modules.items() if name in OPTIONAL_MODULES
    )
    detail = "\n".join(lines)
    if required_failed:
        return _Result("fail", detail, "a blocked or missing native file stops the import: check crt_dlls first, then give this report to IT")
    return _Result("warn" if optional_broken or signer_note else "ok", detail)


@_check()
def sqlite_features() -> _Result:
    conn = sqlite3.connect(":memory:")
    try:
        options = {row[0] for row in conn.execute("PRAGMA compile_options")}
    finally:
        conn.close()
    fts5 = "ENABLE_FTS5" in options
    extensions = hasattr(sqlite3.Connection, "enable_load_extension")
    detail = f"SQLite {sqlite3.sqlite_version}: FTS5 {'yes' if fts5 else 'no'}, loadable extensions {'yes' if extensions else 'no'}"
    if fts5 and extensions:
        return _Result("ok", detail)
    return _Result("fail", detail, "this Python build cannot run the index; use another build, for example the python.org installer for the current user")


@_check(windows_only=True)
def ocr_languages() -> _Result:
    try:
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as exc:
        return _Result("skip", f"Windows OCR projection not importable ({exc})")
    tags = [language.language_tag for language in OcrEngine.available_recognizer_languages]
    if not tags:
        return _Result("warn", "no Windows OCR languages installed", "pip install emailsupersearch[ocr] adds the bundled OCR engine")
    return _Result("ok", ", ".join(tags))


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _nearest_existing(path: Path) -> Path:
    path = path.absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


@_check(windows_only=True)
def memory_disk(root: Path) -> _Result:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    total, available = status.ullTotalPhys / GB, status.ullAvailPhys / GB
    free_disk = shutil.disk_usage(_nearest_existing(root)).free / GB
    detail = f"RAM total {total:.1f} GB, available {available:.1f} GB; free disk {free_disk:.1f} GB"
    problems = []
    if total < MIN_RAM_GB:
        problems.append(f"less than {MIN_RAM_GB} GB RAM")
    if free_disk < MIN_FREE_DISK_GB:
        problems.append(f"less than {MIN_FREE_DISK_GB} GB free disk")
    if problems:
        return _Result("warn", f"{detail} ({'; '.join(problems)})")
    return _Result("ok", detail)


@_check()
def data_root(root: Path) -> _Result:
    reason = paths.validate_data_root(root)
    if reason:
        return _Result("fail", f"{root}: {reason}", "set EMAILSUPERSEARCH_DATA_ROOT to a local folder outside OneDrive, Documents and Desktop")
    return _Result("ok", str(root))


@_check(windows_only=True)
def msg_handler() -> _Result:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MSG_HANDLER_KEY) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except FileNotFoundError:
        return _Result("warn", "no .msg handler registered; Open in Outlook may not work")
    return _Result("ok", f"ProgId {prog_id}")


@_check()
def models_package() -> _Result:
    # Imported here: the embedder pulls in onnxruntime, which doctor must survive losing.
    from emailsupersearch.index.embedder import load_default_embedder

    embedder = load_default_embedder()
    if embedder is None:
        return _Result("fail", "emailsupersearch-models-en is missing or incomplete; semantic search is off", "pip install emailsupersearch[nlp]")
    return _Result("ok", f"embedder {embedder.id}, {embedder.dim} dimensions")


def degraded_features(checks: list[Check], rapidocr_loaded: bool) -> list[str]:
    """Names the features that will run in degraded mode, given the finished checks."""
    status = {c.name: c.status for c in checks}
    degraded = []
    if status.get("models_package") != "ok":
        degraded.append("semantic search")
    if status.get("ocr_languages") != "ok" and not rapidocr_loaded:
        degraded.append("ocr")
    return degraded


def run_doctor(root: Path | None = None) -> DoctorReport:
    root = paths.data_root() if root is None else root
    probe = _probe_modules()
    checks = [
        python_build(),
        path_headroom(),
        crt_dlls(),
        vulkan_loader(),
        native_imports(probe),
        sqlite_features(),
        ocr_languages(),
        memory_disk(root),
        data_root(root),
        msg_handler(),
        models_package(),
    ]
    rapidocr_loaded = bool(probe.modules.get("rapidocr", {}).get("ok"))
    return DoctorReport(checks, degraded_features(checks, rapidocr_loaded), int(time.time()))


def _redact(text: str) -> str:
    for value in (os.environ.get("USERPROFILE"), os.environ.get("USERNAME")):
        if value:
            text = re.sub(re.escape(value), "<user>", text, flags=re.IGNORECASE)
    return text


def _redacted(r: DoctorReport) -> DoctorReport:
    checks = [Check(c.name, c.status, _redact(c.detail), _redact(c.remedy)) for c in r.checks]
    return DoctorReport(checks, [_redact(d) for d in r.degraded], r.generated_at)


def render_report(r: DoctorReport) -> str:
    r = _redacted(r)
    lines = []
    for c in r.checks:
        first, *rest = c.detail.splitlines() or [""]
        lines.append(f"[{c.status.upper()}] {c.name}: {first}")
        lines.extend(f"    {line}" for line in rest)
        if c.remedy:
            lines.append(f"  remedy: {c.remedy}")
    lines.append(f"Degraded: {', '.join(r.degraded) or 'none'}")
    lines.append(f"Generated: {datetime.fromtimestamp(r.generated_at, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return "\n".join(lines)


def report_json(r: DoctorReport) -> str:
    return json.dumps(dataclasses.asdict(_redacted(r)), indent=2)
