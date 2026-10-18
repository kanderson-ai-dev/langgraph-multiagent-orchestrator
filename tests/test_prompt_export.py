"""Prompt export — runtime candidates round-trip to git YAML seeds.

A candidate created through the API lives only in the SQLite overlay.
``export_yaml`` renders it in the seed shape so it can be committed under
``prompts/<name>/v<N>.yaml``; on the next startup the file is adopted as
the source (same template hash) or startup fails loudly (hash conflict).
"""

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app
from app.services.prompt_registry import (
    DEFAULT_PROMPTS_DIR,
    PromptRegistry,
    PromptRegistryError,
)


@pytest.fixture()
def temp_prompts(tmp_path: Path) -> Path:
    """A writable copy of the seed tree for export/reload tests."""
    dst = tmp_path / "prompts"
    shutil.copytree(DEFAULT_PROMPTS_DIR, dst)
    return dst


def _candidate(
    registry: PromptRegistry, name: str = "writer", tag: str = "export-test"
) -> Any:
    return registry.create_candidate(
        name, f"system: test candidate {tag}", changelog=f"for {tag}",
        author="tester",
    )


async def test_export_round_trip_file_adopted_as_yaml_source(
    tmp_path: Path, temp_prompts: Path
) -> None:
    db = str(tmp_path / "r.sqlite")
    reg = PromptRegistry.load(temp_prompts, db_path=db)
    pv = _candidate(reg)
    body = reg.export_yaml(pv.name, pv.version)

    doc = yaml.safe_load(body)
    assert doc["status"] == "candidate"  # non-promoted exports as candidate
    assert doc["name"] == pv.name and doc["version"] == pv.version
    assert doc["template"] == pv.template

    target = temp_prompts / pv.name / f"v{pv.version}.yaml"
    target.write_text(body, encoding="utf-8")
    reg2 = PromptRegistry.load(temp_prompts, db_path=db)
    pv2 = reg2.get(pv.name, pv.version)
    assert pv2.template == pv.template and pv2.sha256 == pv.sha256
    assert pv2.source == "yaml"  # identical hash → file adopted, not duplicated


async def test_conflicting_yaml_seed_fails_fast(
    tmp_path: Path, temp_prompts: Path
) -> None:
    """A committed vN.yaml whose template differs from the runtime row
    must fail startup — never silently shadow one of the two texts."""
    db = str(tmp_path / "r.sqlite")
    reg = PromptRegistry.load(temp_prompts, db_path=db)
    pv = _candidate(reg, tag="conflict")
    target = temp_prompts / pv.name / f"v{pv.version}.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "name": pv.name, "version": pv.version, "status": "candidate",
                "author": "x", "created_at": "2026-01-01T00:00:00Z",
                "target_model": "", "variables": [], "changelog": "different",
                "template": "completely different body",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PromptRegistryError, match="conflicts"):
        PromptRegistry.load(temp_prompts, db_path=db)


async def test_exported_yaml_is_seed_shaped(tmp_path: Path) -> None:
    reg = PromptRegistry.load(db_path=":memory:")
    pv = _candidate(reg, tag="shape")
    doc = yaml.safe_load(reg.export_yaml(pv.name, pv.version))
    assert set(doc) == {
        "name", "version", "status", "author", "created_at",
        "target_model", "variables", "changelog", "template",
    }


async def test_export_endpoint_rbac_and_body(tmp_path: Path) -> None:
    from app.core.config import Settings, get_settings
    from app.core.security import hash_password, issue_token
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        jwt_secret_key="s" * 64,
        admin_password_hash=hash_password("pw"),
    )
    db = str(tmp_path / "r.sqlite")
    reg = PromptRegistry.load(db_path=db)
    pv = _candidate(reg, tag="rbac")

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.prompt_registry = reg
    try:
        with TestClient(app) as client:
            url = f"/api/v1/prompts/writer/versions/{pv.version}/yaml"
            assert client.get(url).status_code == 401
            token = issue_token(settings, subject="v", role="viewer")
            r = client.get(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/yaml")
            doc = yaml.safe_load(r.text)
            assert doc["version"] == pv.version and doc["name"] == "writer"
            assert f"v{pv.version}.yaml" in r.headers["content-disposition"]
            assert client.get(
                "/api/v1/prompts/writer/versions/9999/yaml",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_export_script_prints_and_refuses_overwrite(
    tmp_path: Path, temp_prompts: Path, capsys: Any, monkeypatch: Any
) -> None:
    db = str(tmp_path / "r.sqlite")
    reg = PromptRegistry.load(temp_prompts, db_path=db)
    pv = _candidate(reg, tag="script")

    import scripts.export_prompt as ep

    monkeypatch.setattr(ep, "DEFAULT_PROMPTS_DIR", temp_prompts)
    monkeypatch.setattr(
        sys, "argv", ["export_prompt.py", "writer", str(pv.version), "--db", db]
    )
    assert ep.main() == 0
    out = capsys.readouterr().out
    assert f"version: {pv.version}" in out

    argv = [
        "export_prompt.py", "writer", str(pv.version), "--write", "--db", db,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert ep.main() == 0
    target = temp_prompts / "writer" / f"v{pv.version}.yaml"
    assert target.exists()
    # Second write must refuse — a committed seed is never rewritten.
    monkeypatch.setattr(sys, "argv", argv)
    assert ep.main() == 1


def test_console_has_export_button() -> None:
    js = TestClient(app).get("/console/js/views/prompts.js").text
    assert "export-yaml" in js and "/yaml" in js
