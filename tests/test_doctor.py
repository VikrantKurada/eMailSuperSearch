import ctypes
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from emailsupersearch.diagnostics import doctor
from emailsupersearch.diagnostics.doctor import (
    Check,
    DoctorReport,
    ProbeResult,
    crt_dlls,
    data_root,
    degraded_features,
    memory_disk,
    models_package,
    native_imports,
    path_headroom,
    render_report,
    report_json,
    run_doctor,
    vulkan_loader,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only check")


def test_run_doctor_has_every_check_and_no_crash():
    r = run_doctor(); names = {c.name for c in r.checks}
    assert names >= {"python_build","path_headroom","crt_dlls","vulkan_loader","native_imports","sqlite_features","ocr_languages","memory_disk","data_root","msg_handler","models_package"}
    assert all(c.status in {"ok","warn","fail","skip"} for c in r.checks)


def test_native_imports_reports_signer_status():
    d = next(c for c in run_doctor().checks if c.name == "native_imports").detail; assert "signed:" in d or "unsigned" in d


def test_render_report_redacts_username(): assert os.environ.get("USERNAME", "\x00") not in render_report(run_doctor())


def test_render_and_json_redact_profile_and_username_case_insensitively(monkeypatch):
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Alice.Smith")
    monkeypatch.setenv("USERNAME", "alice.smith")
    r = DoctorReport(
        [Check("data_root", "ok", r"c:\users\ALICE.SMITH\AppData\Local\eMailSuperSearch", remedy="ask ALICE.smith")],
        [],
        0,
    )
    for text in (render_report(r), report_json(r)):
        assert "alice" not in text.lower() and "<user>" in text
    assert json.loads(report_json(r))["checks"][0]["detail"] == r"<user>\AppData\Local\eMailSuperSearch"


def test_render_report_lists_every_check_remedy_degraded_and_time():
    r = DoctorReport(
        [Check("crt_dlls", "fail", "vcomp140.dll missing", remedy="pip install msvc-runtime"), Check("sqlite_features", "ok", "SQLite 3.45")],
        ["semantic search", "ocr"],
        1_700_000_000,
    )
    lines = render_report(r).splitlines()
    assert "[FAIL] crt_dlls: vcomp140.dll missing" in lines
    assert "  remedy: pip install msvc-runtime" in lines
    assert "[OK] sqlite_features: SQLite 3.45" in lines
    assert "Degraded: semantic search, ocr" in lines
    assert any(line.startswith("Generated: 2023-11-14") for line in lines)


def test_degraded_features_names_semantic_search_and_ocr():
    ok, bad = Check("models_package", "ok", ""), Check("models_package", "warn", "")
    ocr_ok, ocr_none = Check("ocr_languages", "ok", "en-US"), Check("ocr_languages", "warn", "")
    assert degraded_features([ok, ocr_ok], rapidocr_loaded=False) == []
    assert degraded_features([bad, ocr_ok], rapidocr_loaded=False) == ["semantic search"]
    assert degraded_features([ok, ocr_none], rapidocr_loaded=False) == ["ocr"]
    assert degraded_features([ok, ocr_none], rapidocr_loaded=True) == []
    assert degraded_features([bad, Check("ocr_languages", "skip", "")], rapidocr_loaded=False) == ["semantic search", "ocr"]


def test_report_json_round_trips():
    r = DoctorReport([Check("x", "warn", "d", remedy="r")], ["ocr"], 5)
    assert json.loads(report_json(r)) == {"checks": [{"name": "x", "status": "warn", "detail": "d", "remedy": "r"}], "degraded": ["ocr"], "generated_at": 5}


@windows_only
def test_crt_dlls_fails_with_remedy_when_one_is_missing(monkeypatch):
    real = ctypes.WinDLL

    def fake(name, *args, **kwargs):
        if name == "vcomp140.dll":
            raise FileNotFoundError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(ctypes, "WinDLL", fake)
    c = crt_dlls()
    assert c.status == "fail" and c.remedy == "pip install msvc-runtime" and "vcomp140.dll" in c.detail


@windows_only
def test_vulkan_loader_missing_is_a_warning_without_remedy(monkeypatch):
    def fake(name, *args, **kwargs):
        raise FileNotFoundError(name)

    monkeypatch.setattr(ctypes, "WinDLL", fake)
    c = vulkan_loader()
    assert c.status == "warn" and c.detail == "Vulkan loader not found; the LLM engine will use the CPU build" and c.remedy == ""


def test_path_headroom_warns_on_long_site_packages(monkeypatch):
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": "C:\\" + "x" * 140})
    assert path_headroom().status == "warn"
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": r"C:\short"})
    assert path_headroom().status == "ok"


def test_data_root_check_fails_with_the_reason(tmp_path):
    bad = data_root(Path(r"C:\Users\x\OneDrive\ess"))
    assert bad.status == "fail" and "OneDrive" in bad.detail
    assert data_root(tmp_path / "ess").status == "ok"


@windows_only
def test_check_never_raises_and_reports_the_exception(monkeypatch):
    def boom(path):
        raise PermissionError("denied")

    monkeypatch.setattr(doctor.shutil, "disk_usage", boom)
    c = memory_disk(Path("."))
    assert c.name == "memory_disk" and c.status == "fail" and "PermissionError: denied" in c.detail


def _probe(**entries):
    return ProbeResult({name: {"ok": e[0], "error": e[1], "missing": e[2], "file": None, "binaries": []} for name, e in entries.items()})


def test_native_imports_status_follows_required_and_optional_modules():
    required = {m: (True, None, False) for m in doctor.REQUIRED_MODULES}
    optional_absent = {"rapidocr": (False, "ModuleNotFoundError: No module named 'rapidocr'", True), "cv2": (False, "ModuleNotFoundError: No module named 'cv2'", True)}
    c = native_imports(_probe(**required, **optional_absent))
    assert c.status == "ok" and "rapidocr: skipped (not installed)" in c.detail

    optional_broken = {"rapidocr": (False, "ImportError: DLL load failed", False), "cv2": (True, None, False)}
    c = native_imports(_probe(**required, **optional_broken))
    assert c.status == "warn" and "rapidocr: not loaded (ImportError: DLL load failed)" in c.detail

    broken = {**required, "onnxruntime": (False, "ImportError: DLL load failed", False)}
    c = native_imports(_probe(**broken, **optional_absent))
    assert c.status == "fail" and "onnxruntime: not loaded (ImportError: DLL load failed)" in c.detail


def test_native_imports_reports_probe_failure_and_unavailable_signer(monkeypatch):
    c = native_imports(ProbeResult({}, "import probe timed out after 60 s"))
    assert c.status == "fail" and c.detail == "import probe timed out after 60 s"

    def unavailable(binaries):
        raise doctor._SignerUnavailable("PowerShell could not start")

    monkeypatch.setattr(doctor, "_signer_statuses", unavailable)
    probe = _probe(**{m: (True, None, False) for m in doctor.REQUIRED_MODULES})
    probe.modules["tokenizers"]["binaries"] = [r"C:\site\tokenizers\tokenizers.pyd"]
    c = native_imports(probe)
    assert c.status == "warn" and "signer check unavailable (PowerShell could not start)" in c.detail
    assert "tokenizers.pyd signer unknown" in c.detail


def test_native_imports_records_signed_and_unsigned_files(monkeypatch):
    binary_a, binary_b = r"C:\site\onnxruntime\a.dll", r"C:\site\onnxruntime\b.pyd"
    monkeypatch.setattr(doctor, "_signer_statuses", lambda binaries: {doctor._path_key(binary_a): "signed:Microsoft Corporation", doctor._path_key(binary_b): "unsigned:NotSigned"})
    probe = _probe(**{m: (True, None, False) for m in doctor.REQUIRED_MODULES})
    probe.modules["onnxruntime"]["binaries"] = [binary_a, binary_b]
    c = native_imports(probe)
    assert c.status == "ok" and "a.dll signed:Microsoft Corporation, b.pyd unsigned:NotSigned" in c.detail


@windows_only
def test_signer_statuses_runs_powershell_on_real_files(tmp_path):
    unsigned = tmp_path / "plain [1]'s.dll"
    unsigned.write_bytes(b"not a real binary")
    system_dll = Path(os.environ["SystemRoot"]) / "System32" / "kernel32.dll"
    got = doctor._signer_statuses([str(unsigned), str(system_dll)])
    assert got[doctor._path_key(str(unsigned))].startswith("unsigned:")
    assert got[doctor._path_key(str(system_dll))] == "signed:Microsoft Windows"


def test_signer_name_prefers_the_common_name():
    assert doctor._signer_name("CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, C=US") == "Microsoft Corporation"
    assert doctor._signer_name('CN="Acme, Inc.", O=Acme') == "Acme, Inc."
    assert doctor._signer_name("O=No Common Name") == "O=No Common Name"
    assert doctor._signer_name(None) == "unknown signer"


def test_probe_script_reports_missing_blocked_and_loaded_modules(monkeypatch, tmp_path):
    package = tmp_path / "blockedpkg"
    (package / "lib").mkdir(parents=True)
    (package / "__init__.py").write_text("raise ImportError('DLL load failed: blocked by policy')")
    (package / "lib" / "native.dll").write_bytes(b"")
    (package / "ext.PYD").write_bytes(b"")
    (package / "notes.txt").write_text("not a binary")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(doctor, "REQUIRED_MODULES", ("json", "blockedpkg"))
    monkeypatch.setattr(doctor, "OPTIONAL_MODULES", ("no_such_module_for_doctor",))
    probe = doctor._probe_modules()
    assert probe.error == ""
    assert probe.modules["json"]["ok"] and probe.modules["json"]["error"] is None
    blocked = probe.modules["blockedpkg"]
    assert not blocked["ok"] and not blocked["missing"] and "DLL load failed" in blocked["error"]
    assert [Path(b).name for b in blocked["binaries"]] == ["ext.PYD", "native.dll"]
    absent = probe.modules["no_such_module_for_doctor"]
    assert not absent["ok"] and absent["missing"]


def test_probe_modules_reports_a_crashed_or_hung_child(monkeypatch):
    def crashed(*args, **kwargs):
        return subprocess.CompletedProcess(args, 3, stdout="noise\n", stderr="Fatal Python error: boom\n")

    monkeypatch.setattr(doctor.subprocess, "run", crashed)
    probe = doctor._probe_modules()
    assert probe.modules == {} and "exit code 3" in probe.error and "Fatal Python error: boom" in probe.error

    def hung(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr(doctor.subprocess, "run", hung)
    assert "timed out after 60 s" in doctor._probe_modules().error


def test_models_package_missing_is_a_warning_and_a_broken_one_is_a_failure(monkeypatch):
    from emailsupersearch.index import embedder

    monkeypatch.setattr(embedder, "load_default_embedder", lambda: None)
    c = models_package()
    assert c.status == "warn" and c.remedy == ""
    assert c.detail == "models package not installed; semantic search disabled (pip install emailsupersearch[nlp])"

    def broken():
        raise RuntimeError("model.onnx is corrupt")

    monkeypatch.setattr(embedder, "load_default_embedder", broken)
    c = models_package()
    assert c.status == "fail" and "RuntimeError: model.onnx is corrupt" in c.detail
