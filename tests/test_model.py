from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from btts_cli.domain.model import (
    DEFAULT_REPO,
    ModelFile,
    ModelScopeProvider,
    download_model,
    read_incomplete_marker,
    read_marker,
)
from btts_cli.output.errors import EngineError, IntegrityError


def model_tree(*, extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    index = {"weight_map": {"embed": "weights.safetensors"}}
    tree = {
        "config.json": b'{"model_type": "fake"}',
        "generation_config.json": b'{"temperature": 1.0}',
        "tokenizer.json": b'{"tokens": []}',
        "tokenizer_config.json": b'{"model_max_length": 32768}',
        "model.safetensors.index.json": json.dumps(index).encode(),
        "weights.safetensors": b"fake weights shard",
        "audio_tokenizer/config.json": b'{"sample_rate": 24000}',
        "audio_tokenizer/model.safetensors": b"fake audio tokenizer",
    }
    if extra:
        tree.update(extra)
    return tree


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeProvider:
    name = "huggingface"
    default_revision = "main"

    def __init__(self, tree: dict[str, bytes], *, diagnostic: bool = False):
        self.tree = tree
        self.diagnostic = diagnostic

    def list_files(self, repo: str, revision: str) -> list[ModelFile]:
        if self.diagnostic:
            print("provider list diagnostic")
        return [
            ModelFile(path=path, size=len(data), sha256=sha256(data))
            for path, data in self.tree.items()
        ]

    def download(self, repo, revision, files, target, *, progress):
        if self.diagnostic:
            print("provider download diagnostic")
        for item in files:
            destination = target / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.tree[item.path])
            progress(f"wrote {item.path}")


class NoNetworkProvider(FakeProvider):
    def list_files(self, repo: str, revision: str) -> list[ModelFile]:
        raise AssertionError("provider must not be contacted")


class CorruptProvider(FakeProvider):
    """Writes the same number of bytes but the wrong content for config.json."""

    def download(self, repo, revision, files, target, *, progress):
        for item in files:
            destination = target / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = self.tree[item.path]
            if item.path == "config.json":
                data = b"x" * len(data)
            destination.write_bytes(data)
            progress(f"wrote {item.path}")


class NoShaCorruptProvider(FakeProvider):
    """Same-size corruption from a mirror that publishes no SHA-256, then fails."""

    def list_files(self, repo: str, revision: str) -> list[ModelFile]:
        return [
            ModelFile(path=path, size=len(data), sha256=None)
            for path, data in self.tree.items()
        ]

    def download(self, repo, revision, files, target, *, progress):
        for item in files:
            destination = target / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = self.tree[item.path]
            if item.path == "config.json":
                data = b"x" * len(data)
            destination.write_bytes(data)
            progress(f"wrote {item.path}")
        raise RuntimeError("simulated provider failure")


def install_fake_huggingface(
    monkeypatch: pytest.MonkeyPatch, tree: dict[str, bytes]
) -> None:
    pytest.importorskip("huggingface_hub")
    import huggingface_hub

    class FakeHfApi:
        def model_info(self, repo_id, revision, files_metadata):
            return SimpleNamespace(
                siblings=[
                    SimpleNamespace(rfilename=path, size=len(data), lfs=None)
                    for path, data in tree.items()
                ]
            )

    def fake_snapshot_download(*, repo_id, revision, local_dir):
        target = Path(local_dir)
        for path, data in tree.items():
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        return str(target)

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeHfApi)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)


@contextlib.contextmanager
def modelscope_server(tree: dict[str, bytes], *, bad_sha: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        requests: list[dict[str, object]] = []

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            Handler.requests.append(
                {
                    "raw": self.path,
                    "path": parsed.path,
                    "query": query,
                    "range": self.headers.get("Range"),
                }
            )
            if parsed.path.endswith("/repo/files"):
                entries = []
                for path, data in tree.items():
                    entries.append(
                        {
                            "Path": path,
                            "Size": len(data),
                            "Sha256": "0" * 64 if path == bad_sha else sha256(data),
                            "Type": "blob",
                        }
                    )
                entries.append(
                    {"Path": "audio_tokenizer", "Size": 0, "Sha256": "", "Type": "tree"}
                )
                body = json.dumps({"Data": {"Files": entries}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path.endswith("/repo"):
                file_path = query.get("FilePath", [""])[0]
                if file_path not in tree:
                    self.send_error(404)
                    return
                data = tree[file_path]
                start = 0
                header = self.headers.get("Range")
                if header and header.startswith("bytes="):
                    start = int(header[len("bytes=") :].split("-")[0])
                body = data[start:]
                self.send_response(206 if start else 200)
                if start:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}"
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def use_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("btts_cli.commands.model.get_provider", lambda source: provider)


def test_huggingface_download_writes_marker_and_verifies(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    install_fake_huggingface(monkeypatch, tree)
    target = isolated / "model"

    result = run(
        "model",
        "download",
        "--source",
        "huggingface",
        "--repo",
        "acme/fake",
        "--to",
        target,
        "--yes",
        "--json",
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == "huggingface"
    assert payload["repo"] == "acme/fake"
    assert payload["revision"] == "main"
    assert payload["files"] == len(tree)
    assert payload["verified"] == len(tree)
    assert payload["total_bytes"] == sum(len(data) for data in tree.values())
    assert "hf" not in result.stdout.lower()

    marker_path = target / ".btts-model.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 1
    assert marker["source"] == "huggingface"
    assert marker["repo"] == "acme/fake"
    assert marker["revision"] == "main"
    assert marker["downloaded_at"].endswith("Z")
    assert set(marker["files"]) == set(tree)
    assert marker["files"]["config.json"] == {
        "size": len(tree["config.json"]),
        "sha256": sha256(tree["config.json"]),
    }

    verify = run("model", "verify", "--model", target, "--deep", "--json")
    assert verify.exit_code == 0, verify.stderr
    report = json.loads(verify.stdout)
    assert report["complete"] is True
    assert report["deep"] is True
    assert report["missing"] == []
    assert report["source"] == "huggingface"
    assert report["revision"] == "main"
    assert report["files_checked"] == len(tree)
    assert report["digest"].startswith("sha256:")

    doctor = run("doctor", "--json", "--model", target)
    assert doctor.exit_code == 0, doctor.stderr
    model = json.loads(doctor.stdout)["model"]
    assert model["marker_found"] is True
    assert model["source"] == "huggingface"
    assert model["repo"] == "acme/fake"
    assert model["revision"] == "main"
    assert model["downloaded_at"] == marker["downloaded_at"]


def test_json_stdout_stays_clean_when_provider_prints(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(model_tree(), diagnostic=True)
    use_provider(monkeypatch, provider)

    result = run(
        "model",
        "download",
        "--to",
        isolated / "model",
        "--yes",
        "--json",
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["verified"] == len(provider.tree)
    assert "diagnostic" not in result.stdout
    assert "provider list diagnostic" in result.stderr
    assert "provider download diagnostic" in result.stderr


def test_modelscope_download_resumes_part_and_encodes_urls(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree(extra={"nested/extra file.bin": b"extra payload"})
    target = isolated / "model"
    target.mkdir()
    partial = tree["config.json"][:5]
    (target / "config.json.part").write_bytes(partial)

    with modelscope_server(tree) as (base_url, handler):
        provider = ModelScopeProvider(base_url=base_url)
        monkeypatch.setattr(
            "btts_cli.commands.model.get_provider", lambda source: provider
        )
        result = run(
            "model",
            "download",
            "--source",
            "modelscope",
            "--to",
            target,
            "--yes",
            "--json",
        )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == "modelscope"
    assert payload["revision"] == "master"
    assert payload["skipped"] == 0
    assert (target / "nested" / "extra file.bin").read_bytes() == b"extra payload"
    assert (target / "config.json").read_bytes() == tree["config.json"]
    assert not list(target.rglob("*.part"))

    ranges = [
        request
        for request in handler.requests
        if request["query"].get("FilePath") == ["config.json"]
    ]
    assert any(request["range"] == f"bytes={len(partial)}-" for request in ranges)
    encoded = [
        request["raw"]
        for request in handler.requests
        if request["query"].get("FilePath") == ["nested/extra file.bin"]
    ]
    assert encoded
    assert "FilePath=nested%2Fextra%20file.bin" in encoded[0]


def test_modelscope_sha_mismatch_exits_5_without_marker(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    with modelscope_server(tree, bad_sha="config.json") as (base_url, _handler):
        provider = ModelScopeProvider(base_url=base_url)
        monkeypatch.setattr(
            "btts_cli.commands.model.get_provider", lambda source: provider
        )
        result = run(
            "model",
            "download",
            "--source",
            "modelscope",
            "--to",
            isolated / "model",
            "--yes",
        )

    assert result.exit_code == 5
    assert "SHA-256 mismatch" in result.stderr
    assert not (isolated / "model" / ".btts-model.json").exists()


def test_nonempty_directory_requires_force(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated / "model"
    target.mkdir()
    (target / "notes.txt").write_text("keep me", encoding="utf-8")
    use_provider(monkeypatch, NoNetworkProvider(model_tree()))

    result = run("model", "download", "--to", target, "--yes")
    assert result.exit_code == 2
    assert "--force" in result.stderr
    assert not (target / ".btts-model.json").exists()


def test_force_keeps_unrelated_files(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated / "model"
    target.mkdir()
    (target / "notes.txt").write_text("keep me", encoding="utf-8")
    (target / "notes.txt.part").write_text("keep partial too", encoding="utf-8")
    provider = FakeProvider(model_tree())
    use_provider(monkeypatch, provider)

    result = run("model", "download", "--to", target, "--force", "--yes")
    assert result.exit_code == 0, result.stderr
    assert (target / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (target / "notes.txt.part").read_text(encoding="utf-8") == "keep partial too"
    assert (target / ".btts-model.json").is_file()


def test_recognizable_partial_download_resumes_without_force(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated / "model"
    target.mkdir()
    (target / "config.json.part").write_bytes(b"partial")
    (target / "README.md").write_text("repo metadata", encoding="utf-8")
    (target / "assets").mkdir()
    (target / "assets" / "logo.png").write_bytes(b"png")
    use_provider(monkeypatch, FakeProvider(model_tree()))

    result = run("model", "download", "--to", target, "--yes")
    assert result.exit_code == 0, result.stderr
    assert (target / "config.json").is_file()
    assert not (target / "config.json.part").exists()


def test_non_tty_without_yes_exits_2_without_network(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, NoNetworkProvider(model_tree()))

    result = run("model", "download", "--to", isolated / "model")
    assert result.exit_code == 2
    assert "--yes" in result.stderr


def test_disk_space_shortfall_exits_2(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_provider(monkeypatch, FakeProvider(model_tree()))
    monkeypatch.setattr(
        "btts_cli.domain.model.shutil.disk_usage",
        lambda path: SimpleNamespace(total=1, used=1, free=0),
    )

    result = run(
        "model",
        "download",
        "--to",
        isolated / "model",
        "--yes",
    )
    assert result.exit_code == 2
    assert "disk space" in result.stderr
    assert not (isolated / "model").exists()


def test_verify_missing_and_hash_mismatch_exit_5(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_huggingface(monkeypatch, model_tree())
    target = isolated / "model"
    download = run("model", "download", "--to", target, "--yes", "--json")
    assert download.exit_code == 0, download.stderr

    config = target / "config.json"
    original = config.read_bytes()
    config.write_bytes(b"x" * len(original))
    shallow = run("model", "verify", "--model", target, "--json")
    assert shallow.exit_code == 0, shallow.stderr
    assert json.loads(shallow.stdout)["complete"] is True

    deep = run("model", "verify", "--model", target, "--deep", "--json")
    assert deep.exit_code == 5
    report = json.loads(deep.stdout)
    assert report["complete"] is False
    assert any("SHA-256 mismatch" in warning for warning in report["warnings"])

    (target / "tokenizer.json").unlink()
    missing = run("model", "verify", "--model", target, "--json")
    assert missing.exit_code == 5
    report = json.loads(missing.stdout)
    assert report["complete"] is False
    assert "tokenizer.json" in report["missing"]


def test_verify_without_marker_is_integrity_only(
    run, isolated: Path
) -> None:
    target = isolated / "model"
    for path, data in model_tree().items():
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    result = run("model", "verify", "--model", target, "--deep", "--json")
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["complete"] is True
    assert report["source"] is None
    assert "marker" in " ".join(report["warnings"]).lower()


def test_doctor_warns_about_incomplete_marked_download(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_huggingface(monkeypatch, model_tree())
    target = isolated / "model"
    download = run("model", "download", "--to", target, "--yes", "--json")
    assert download.exit_code == 0, download.stderr
    (target / "tokenizer.json").unlink()

    result = run("doctor", "--json", "--model", target)
    assert result.exit_code == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["model"]["marker_found"] is True
    assert any("model download" in warning for warning in report["warnings"])


def test_marker_round_trip_and_invalid_marker(isolated: Path) -> None:
    target = isolated / "model"
    target.mkdir()
    assert read_marker(target) is None

    marker = {
        "schema_version": 1,
        "source": "modelscope",
        "repo": "acme/fake",
        "revision": "master",
        "downloaded_at": "2026-11-03T12:34:56Z",
        "files": {"config.json": {"size": 2, "sha256": "ab"}},
    }
    (target / ".btts-model.json").write_text(json.dumps(marker), encoding="utf-8")
    loaded = read_marker(target)
    assert loaded is not None
    assert loaded.source == "modelscope"
    assert loaded.files["config.json"].size == 2

    marker["files"] = {"../escape.bin": {"size": 1, "sha256": "ab"}}
    (target / ".btts-model.json").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(IntegrityError):
        read_marker(target)


def test_download_rejects_directory_symlink(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated / "model"
    target.mkdir()
    outside = isolated / "outside"
    outside.mkdir()
    (target / "audio_tokenizer").symlink_to(outside, target_is_directory=True)
    use_provider(monkeypatch, FakeProvider(model_tree()))

    result = run("model", "download", "--to", target, "--yes")
    assert result.exit_code == 5
    assert "symlink" in result.stderr
    assert list(outside.iterdir()) == []
    assert not (target / ".btts-model.json").exists()


def test_download_rejects_final_file_symlink(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated / "model"
    target.mkdir()
    outside = isolated / "outside.txt"
    outside.write_text("do not touch", encoding="utf-8")
    (target / "config.json").symlink_to(outside)
    use_provider(monkeypatch, FakeProvider(model_tree()))

    result = run("model", "download", "--to", target, "--yes")
    assert result.exit_code == 5
    assert "symlink" in result.stderr
    assert outside.read_text(encoding="utf-8") == "do not touch"
    assert not (target / ".btts-model.json").exists()


def test_download_rejects_target_symlink_without_network(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = isolated / "outside"
    outside.mkdir()
    target = isolated / "model"
    target.symlink_to(outside, target_is_directory=True)
    use_provider(monkeypatch, NoNetworkProvider(model_tree()))

    result = run("model", "download", "--to", target, "--yes")
    assert result.exit_code == 5
    assert "symlink" in result.stderr
    assert list(outside.iterdir()) == []


def test_huggingface_rejects_preexisting_tree_symlink(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_huggingface(monkeypatch, model_tree())
    target = isolated / "model"
    target.mkdir()
    outside = isolated / "outside.txt"
    outside.write_text("do not touch", encoding="utf-8")
    (target / "notes.txt").symlink_to(outside)

    result = run("model", "download", "--to", target, "--force", "--yes")
    assert result.exit_code == 5
    assert "symlink" in result.stderr
    assert outside.read_text(encoding="utf-8") == "do not touch"
    assert not (target / "config.json").exists()
    assert not (target / ".btts-model.json").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../escaped-domain.bin",
        "dir\\file.bin",
        "bad\x00name.bin",
        ".",
        str(Path("/tmp") / "escaped-domain-abs.bin"),
    ],
)
def test_download_model_rejects_unsafe_domain_paths(
    isolated: Path, path: str
) -> None:
    target = isolated / "model"
    files = [ModelFile(path=path, size=1)]
    with pytest.raises(EngineError):
        download_model(
            provider=FakeProvider(model_tree()),
            repo="acme/fake",
            revision="main",
            target=target,
            files=files,
            progress=lambda message: None,
        )
    assert not target.exists()
    assert not (Path("/tmp") / "escaped-domain-abs.bin").exists()


def test_download_model_rejects_invalid_domain_manifests(isolated: Path) -> None:
    target = isolated / "model"
    cases = [
        [ModelFile(path="weights.bin", size=-1)],
        [],
        [
            ModelFile(path="weights.bin", size=1),
            ModelFile(path="weights.bin", size=1),
        ],
    ]
    for files in cases:
        with pytest.raises(EngineError):
            download_model(
                provider=FakeProvider(model_tree()),
                repo="acme/fake",
                revision="main",
                target=target,
                files=files,
                progress=lambda message: None,
            )
    assert not target.exists()


def test_force_refresh_failure_invalidates_old_marker(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    target = isolated / "model"
    use_provider(monkeypatch, FakeProvider(tree))
    first = run("model", "download", "--to", target, "--yes", "--json")
    assert first.exit_code == 0, first.stderr
    assert (target / ".btts-model.json").is_file()

    use_provider(monkeypatch, CorruptProvider(tree))
    refresh = run("model", "download", "--to", target, "--force", "--yes")
    assert refresh.exit_code == 5
    assert "SHA-256 mismatch" in refresh.stderr
    assert not (target / ".btts-model.json").exists()

    doctor = run("doctor", "--json", "--model", target)
    assert doctor.exit_code == 0, doctor.stderr
    report = json.loads(doctor.stdout)
    assert report["model"]["marker_found"] is False
    assert report["model"]["missing"]
    assert report["warnings"]

    shallow = run("model", "verify", "--model", target, "--json")
    assert shallow.exit_code == 5
    assert json.loads(shallow.stdout)["complete"] is False

    deep = run("model", "verify", "--model", target, "--deep", "--json")
    assert deep.exit_code == 5
    assert json.loads(deep.stdout)["complete"] is False


def test_download_rejects_symlink_loop_before_writing(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = isolated / "left"
    right = isolated / "right"
    left.symlink_to(right, target_is_directory=True)
    right.symlink_to(left, target_is_directory=True)
    use_provider(monkeypatch, FakeProvider(model_tree()))

    result = run("model", "download", "--to", left / "model", "--yes")

    assert result.exit_code == 5
    assert "symlink loop" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (right / "model").exists()


def test_no_sha_provider_failure_marks_download_incomplete(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    target = isolated / "model"
    use_provider(monkeypatch, NoShaCorruptProvider(tree))

    failed = run("model", "download", "--to", target, "--yes", "--json")
    assert failed.exit_code == 3
    assert "simulated provider failure" in failed.stderr
    assert not (target / ".btts-model.json").exists()
    state_path = target / ".btts-model.incomplete.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["source"] == "huggingface"
    assert state["repo"] == DEFAULT_REPO
    assert state["revision"] == "main"
    assert state["started_at"].endswith("Z")
    assert "simulated provider failure" in state["error"]
    # The untrusted same-size file is removed rather than kept as complete.
    assert not (target / "config.json").exists()

    doctor = run("doctor", "--json", "--model", target)
    assert doctor.exit_code == 0, doctor.stderr
    doctor_report = json.loads(doctor.stdout)
    assert doctor_report["model"]["marker_found"] is False
    assert doctor_report["model"]["download_incomplete"] is True
    assert doctor_report["model"]["download_started_at"].endswith("Z")
    assert any("download" in warning for warning in doctor_report["warnings"])

    shallow = run("model", "verify", "--model", target, "--json")
    assert shallow.exit_code == 5
    shallow_report = json.loads(shallow.stdout)
    assert shallow_report["complete"] is False
    assert any("did not finish" in warning for warning in shallow_report["warnings"])

    deep = run("model", "verify", "--model", target, "--deep", "--json")
    assert deep.exit_code == 5
    assert json.loads(deep.stdout)["complete"] is False

    # The explicit state still allows a plain resume without --force.
    use_provider(monkeypatch, FakeProvider(tree))
    resumed = run("model", "download", "--to", target, "--yes", "--json")
    assert resumed.exit_code == 0, resumed.stderr
    assert (target / ".btts-model.json").is_file()
    assert not state_path.exists()


def test_doctor_reports_stale_incomplete_state_but_marker_wins(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_huggingface(monkeypatch, model_tree())
    target = isolated / "model"
    download = run("model", "download", "--to", target, "--yes", "--json")
    assert download.exit_code == 0, download.stderr
    stale = {
        "schema_version": 1,
        "source": "huggingface",
        "repo": DEFAULT_REPO,
        "revision": "main",
        "started_at": "2026-11-04T12:34:56Z",
        "error": None,
    }
    (target / ".btts-model.incomplete.json").write_text(
        json.dumps(stale), encoding="utf-8"
    )

    doctor = run("doctor", "--json", "--model", target)
    assert doctor.exit_code == 0, doctor.stderr
    report = json.loads(doctor.stdout)
    assert report["model"]["marker_found"] is True
    assert report["model"]["download_incomplete"] is True
    assert report["model"]["download_state_stale"] is True
    assert any("stale download state" in warning for warning in report["warnings"])

    verify = run("model", "verify", "--model", target, "--deep", "--json")
    assert verify.exit_code == 0, verify.stderr
    assert json.loads(verify.stdout)["complete"] is True


def test_incomplete_marker_round_trip_and_invalid_state(isolated: Path) -> None:
    target = isolated / "model"
    target.mkdir()
    assert read_incomplete_marker(target) is None

    state = {
        "schema_version": 1,
        "source": "modelscope",
        "repo": "acme/fake",
        "revision": "master",
        "started_at": "2026-11-04T12:34:56Z",
        "error": None,
    }
    (target / ".btts-model.incomplete.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    loaded = read_incomplete_marker(target)
    assert loaded is not None
    assert loaded.source == "modelscope"
    assert loaded.error is None

    state["schema_version"] = 99
    (target / ".btts-model.incomplete.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    with pytest.raises(IntegrityError):
        read_incomplete_marker(target)


def _run_modelscope_download(
    run,
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    tree: dict[str, bytes],
    *,
    target: Path,
    json_output: bool = False,
):
    with modelscope_server(tree) as (base_url, _handler):
        provider = ModelScopeProvider(base_url=base_url)
        monkeypatch.setattr(
            "btts_cli.commands.model.get_provider", lambda source: provider
        )
        args = [
            "model",
            "download",
            "--source",
            "modelscope",
            "--to",
            target,
            "--yes",
        ]
        if json_output:
            args.append("--json")
        return run(*args)


def test_download_maps_target_mkdir_oserror_to_exit_3(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    target = isolated / "model"
    real_mkdir = Path.mkdir

    def fail_target_mkdir(self, *args, **kwargs):
        if self == target:
            raise PermissionError("simulated target mkdir failure")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_target_mkdir)
    result = _run_modelscope_download(
        run, isolated, monkeypatch, tree, target=target, json_output=True
    )
    assert result.exit_code == 3
    assert "cannot create model directory" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout.strip() == "" or json.loads(result.stdout)


def test_download_maps_child_mkdir_oserror_to_exit_3(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    target = isolated / "model"
    target.mkdir()
    child = target / "audio_tokenizer"
    real_mkdir = Path.mkdir

    def fail_child_mkdir(self, *args, **kwargs):
        if self == child:
            raise PermissionError("simulated child mkdir failure")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_child_mkdir)
    result = _run_modelscope_download(run, isolated, monkeypatch, tree, target=target)
    assert result.exit_code == 3
    assert "cannot create model directory" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (target / ".btts-model.json").exists()


def test_download_maps_os_replace_oserror_to_exit_3(
    run, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = model_tree()
    target = isolated / "model"
    real_replace = Path.replace

    def fail_part_replace(self, destination):
        if self.name.endswith(".part"):
            raise PermissionError("simulated os.replace failure")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_part_replace)
    result = _run_modelscope_download(
        run, isolated, monkeypatch, tree, target=target, json_output=True
    )
    assert result.exit_code == 3
    assert "cannot commit model file" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout.strip() == "" or json.loads(result.stdout)
    assert not (target / ".btts-model.json").exists()


def test_model_commands_do_not_import_torch_or_huggingface() -> None:
    code = (
        "import btts_cli.commands.model, sys; "
        "print('torch' in sys.modules, 'huggingface_hub' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False"


def test_model_help(run) -> None:
    root = run("model", "--help")
    assert root.exit_code == 0
    assert "download" in root.output
    assert "verify" in root.output

    download = run("model", "download", "--help")
    assert download.exit_code == 0
    for option in ("--source", "--repo", "--revision", "--to", "--force", "--yes", "--json"):
        assert option in download.output

    verify = run("model", "verify", "--help")
    assert verify.exit_code == 0
    for option in ("--model", "--deep", "--json"):
        assert option in verify.output


@pytest.mark.skipif(
    os.environ.get("BTTS_RUN_DOWNLOAD") != "1",
    reason="set BTTS_RUN_DOWNLOAD=1 to exercise the real ModelScope file list",
)
def test_modelscope_online_manifest() -> None:
    files = ModelScopeProvider(timeout=30).list_files(DEFAULT_REPO, "master")
    paths = {item.path for item in files}
    assert "config.json" in paths
    assert "model.safetensors.index.json" in paths
    config = next(item for item in files if item.path == "config.json")
    assert config.size > 0
    assert config.sha256
