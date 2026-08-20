from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import watson.config as config_module
from watson.web import create_app


TOKEN = "test-session-token"


def test_navigation_never_accesses_system_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "_credential_backend",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected Keychain access")),
    )
    app = create_app(token=TOKEN, data_dir=tmp_path)
    project = app.state.project_store.create("No implicit credential access")
    app.state.project_store.add_stream(project.id, "article.txt", BytesIO(b"article"))

    with TestClient(app) as client:
        responses = [
            client.get(f"/{TOKEN}/", headers={"host": "127.0.0.1"}),
            client.get(f"/{TOKEN}/projects/{project.id}", headers={"host": "127.0.0.1"}),
            client.get(
                f"/{TOKEN}/projects/{project.id}/settings",
                headers={"host": "127.0.0.1"},
            ),
            client.get(f"/{TOKEN}/settings", headers={"host": "127.0.0.1"}),
        ]
        run_attempt = client.post(
            f"/{TOKEN}/projects/{project.id}/runs",
            data={"action": "all", "retry_mode": "failed"},
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )

    assert all(response.status_code == 200 for response in responses)
    assert run_attempt.status_code == 303
    assert "Explicitly" in run_attempt.headers["location"]
    assert "never accesses" in responses[-1].text


def test_keychain_read_requires_explicit_load_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Backend:
        def get_password(self, service: str, username: str) -> str:
            calls.append("get")
            return "explicit-secret"

        def set_password(self, service: str, username: str, password: str) -> None:
            calls.append("set")

        def delete_password(self, service: str, username: str) -> None:
            calls.append("delete")

    monkeypatch.setattr(config_module, "_credential_backend", lambda: Backend())
    app = create_app(token=TOKEN, data_dir=tmp_path)
    app.state.config.write_config({"gemini_api_key_storage": "keychain"})

    with TestClient(app) as client:
        settings = client.get(
            f"/{TOKEN}/settings",
            headers={"host": "127.0.0.1"},
        )
        assert calls == []
        loaded = client.post(
            f"/{TOKEN}/credentials/load",
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )

    assert settings.status_code == 200
    assert "Load from" in settings.text
    assert loaded.status_code == 303
    assert "/settings" in loaded.headers["location"]
    assert calls == ["get"]
    assert app.state.config.get_session_api_key() == "explicit-secret"


def test_routes_require_session_token_and_safe_host(tmp_path: Path) -> None:
    with TestClient(create_app(token=TOKEN, data_dir=tmp_path)) as client:
        assert client.get(f"/{TOKEN}/", headers={"host": "127.0.0.1"}).status_code == 200
        assert client.get("/wrong/", headers={"host": "127.0.0.1"}).status_code == 404
        assert client.get(f"/{TOKEN}/", headers={"host": "attacker.example"}).status_code == 400


def test_cross_origin_mutations_are_rejected(tmp_path: Path) -> None:
    with TestClient(create_app(token=TOKEN, data_dir=tmp_path)) as client:
        response = client.post(
            f"/{TOKEN}/projects",
            data={"name": "Blocked"},
            headers={"host": "127.0.0.1", "origin": "https://attacker.example"},
        )

    assert response.status_code == 403


def test_loopback_alias_origin_is_accepted_on_same_port(tmp_path: Path) -> None:
    with TestClient(create_app(token=TOKEN, data_dir=tmp_path)) as client:
        response = client.post(
            f"/{TOKEN}/projects",
            data={"name": "Allowed"},
            headers={
                "host": "127.0.0.1:8765",
                "origin": "http://localhost:8765",
                "sec-fetch-site": "cross-site",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303


def test_loopback_origin_with_rewritten_port_is_accepted(tmp_path: Path) -> None:
    with TestClient(create_app(token=TOKEN, data_dir=tmp_path)) as client:
        response = client.post(
            f"/{TOKEN}/projects",
            data={"name": "Proxied local project"},
            headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1",
                "sec-fetch-site": "same-site",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303


def test_opaque_browser_origin_is_accepted_with_token(tmp_path: Path) -> None:
    with TestClient(create_app(token=TOKEN, data_dir=tmp_path)) as client:
        response = client.post(
            f"/{TOKEN}/projects",
            data={"name": "Embedded browser project"},
            headers={
                "host": "127.0.0.1:8765",
                "origin": "null",
                "sec-fetch-site": "none",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303


def test_create_upload_refresh_and_download_flow(tmp_path: Path) -> None:
    app = create_app(token=TOKEN, data_dir=tmp_path)
    with TestClient(app) as client:
        created = client.post(
            f"/{TOKEN}/projects",
            data={"name": "Paths with spaces 数据"},
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )
        location = created.headers["location"]
        project_id = location.rstrip("/").split("/")[-1]
        uploaded = client.post(
            f"/{TOKEN}/projects/{project_id}/inputs",
            files=[("files", ("article.txt", b"article", "text/plain"))],
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )
        refreshed = client.get(location, headers={"host": "127.0.0.1"})

    assert created.status_code == 303
    assert uploaded.status_code == 303
    assert refreshed.status_code == 200
    assert "Paths with spaces 数据" in refreshed.text
    assert "article.txt" in refreshed.text
    assert "data-dropzone" in refreshed.text
    assert "data-file-picker" in refreshed.text
    assert "Drop files here" in refreshed.text


def test_global_settings_are_shared_across_projects(tmp_path: Path) -> None:
    app = create_app(token=TOKEN, data_dir=tmp_path)
    with TestClient(app) as client:
        saved = client.post(
            f"/{TOKEN}/settings",
            data={"model": "gemini-custom", "thinking_level": "medium"},
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )
        page = client.get(f"/{TOKEN}/settings", headers={"host": "127.0.0.1"})

    assert saved.status_code == 303
    assert page.status_code == 200
    assert "gemini-custom" in page.text
    assert 'value="medium" selected' in page.text.replace("\n", " ")


def test_rename_clear_output_and_delete_project(tmp_path: Path) -> None:
    app = create_app(token=TOKEN, data_dir=tmp_path)
    project = app.state.project_store.create("Original name")
    paths = app.state.project_store.paths(project.id)
    (paths.state / "inventory.json").write_text("{}", encoding="utf-8")
    (paths.outputs / "watson-inventory-report.md").write_text("# report", encoding="utf-8")

    with TestClient(app) as client:
        renamed = client.post(
            f"/{TOKEN}/projects/{project.id}/rename",
            data={"name": "Renamed project"},
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )
        project_page = client.get(f"/{TOKEN}/projects/{project.id}", headers={"host": "127.0.0.1"})
        cleared = client.post(
            f"/{TOKEN}/projects/{project.id}/clear-output",
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )
        deleted = client.post(
            f"/{TOKEN}/projects/{project.id}/delete",
            headers={"host": "127.0.0.1"},
            follow_redirects=False,
        )
        gone = client.get(f"/{TOKEN}/projects/{project.id}", headers={"host": "127.0.0.1"})

    assert renamed.status_code == 303
    assert "Renamed project" in project_page.text
    assert cleared.status_code == 303
    assert not (paths.state / "inventory.json").exists()
    assert deleted.status_code == 303
    assert not paths.root.exists()
    assert gone.status_code == 404


def test_missing_projects_and_unsafe_downloads_do_not_leak_paths(tmp_path: Path) -> None:
    app = create_app(token=TOKEN, data_dir=tmp_path)
    with TestClient(app) as client:
        missing = client.get(f"/{TOKEN}/projects/{'a' * 32}", headers={"host": "127.0.0.1"})
        traversal = client.get(
            f"/{TOKEN}/projects/{'a' * 32}/downloads/..%2Fproject.json",
            headers={"host": "127.0.0.1"},
        )

    assert missing.status_code == 404
    assert traversal.status_code in {400, 404}
    assert str(tmp_path) not in missing.text + traversal.text


def test_reports_and_specific_findings_are_readable_in_browser(tmp_path: Path) -> None:
    app = create_app(token=TOKEN, data_dir=tmp_path)
    project = app.state.project_store.create("Readable results")
    paths = app.state.project_store.paths(project.id)
    (paths.state / "inventory.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-02T12:00:00Z",
                "model": "test-model",
                "files": [{"path": "article.pdf"}, {"path": "prereg.pdf"}],
                "documents": [
                    {
                        "file_path": "article.pdf",
                        "document_type": "article",
                        "confidence": 0.95,
                        "rationale": "This is the published article.",
                    }
                ],
                "studies": [
                    {
                        "study_id": "study-1",
                        "label": "Study 1",
                        "description": "Primary experiment",
                        "article_file_path": "article.pdf",
                        "article_location": "Methods",
                        "article_says_preregistered": True,
                    }
                ],
                "preregistration_matches": [
                    {
                        "study_id": "study-1",
                        "matched_file_path": "prereg.pdf",
                        "match_status": "matched",
                        "confidence": 0.9,
                        "rationale": "Study labels and sample sizes align.",
                    }
                ],
                "review_notes": ["Confirm the article classification."],
            }
        ),
        encoding="utf-8",
    )
    (paths.state / "deviation-checks.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-02T12:30:00Z",
                "model": "test-model",
                "reports": [
                    {
                        "study_id": "study-1",
                        "study_label": "Study 1",
                        "status": "completed",
                        "article_file_path": "article.pdf",
                        "preregistration_file_path": "prereg.pdf",
                        "apa_citation": "Researcher, A. (2026). Example article.",
                        "overall_assessment": "The article partially adhered to the plan.",
                        "review_notes": [],
                        "supplemental_file_paths": ["supplement.pdf"],
                        "preregistration_inventory": {
                            "items": [
                                {
                                    "item_id": "P1",
                                    "category": "exclusion_criteria",
                                    "statement": "Exclude duplicate responses.",
                                    "specificity": "unspecified",
                                }
                            ]
                        },
                        "article_inventory": {
                            "items": [
                                {
                                    "item_id": "A1",
                                    "category": "exclusion_criteria",
                                    "statement": "Excluded attention-check failures.",
                                }
                            ]
                        },
                        "missing_preregistered_items": [
                            {
                                "prereg_item_id": "P2",
                                "category": "analysis_model",
                                "preregistered_plan": "A mediation analysis was promised.",
                                "evidence": "Preregistration p. 4.",
                                "confidence": "high",
                            }
                        ],
                        "unregistered_article_items": [
                            {
                                "article_item_id": "A1",
                                "category": "exclusion_criteria",
                                "article_report": "An attention check was applied.",
                                "framing": "confirmatory",
                                "evidence": "Article p. 8.",
                            }
                        ],
                        "degrees_of_freedom": [
                            {
                                "prereg_item_id": "P1",
                                "category": "exclusion_criteria",
                                "underspecification": "No outlier cutoff was named.",
                                "plausible_alternatives": "2 SD or 3 SD.",
                                "severity": "high",
                            }
                        ],
                        "deviations": [
                            {
                                "deviation_type": "exclusion_criteria",
                                "summary": "An additional exclusion rule was used.",
                                "preregistered_plan": "Exclude duplicate responses only.",
                                "article_report": "Attention-check failures were also excluded.",
                                "evidence": "Preregistration p. 3; article p. 8.",
                                "confidence": "high",
                                "disclosed": "no",
                                "explanation_given": "no",
                                "robustness_check": "not reported",
                            }
                        ],
                    }
                ],
                "skipped_studies": [],
                "review_notes": [],
            }
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        project_page = client.get(
            f"/{TOKEN}/projects/{project.id}", headers={"host": "127.0.0.1"}
        )
        deviation_report = client.get(
            f"/{TOKEN}/projects/{project.id}/reports/preregistration",
            headers={"host": "127.0.0.1"},
        )
        inventory_report = client.get(
            f"/{TOKEN}/projects/{project.id}/reports/inventory",
            headers={"host": "127.0.0.1"},
        )

    assert project_page.status_code == 200
    assert "Read report" in project_page.text
    assert "An additional exclusion rule was used." in project_page.text
    assert "Preregistration p. 3; article p. 8." in project_page.text
    assert "1. Missing preregistered items" in project_page.text
    assert "2. Reported but not preregistered" in project_page.text
    assert "3. Deviations" in project_page.text
    assert "4. Preregistration degrees of freedom" in project_page.text
    assert "A mediation analysis was promised." in project_page.text
    assert "No outlier cutoff was named." in project_page.text
    assert "4 findings" in project_page.text
    assert "Missing prereg items" in project_page.text
    assert "Degrees of freedom" in project_page.text
    assert deviation_report.status_code == 200
    assert "Preregistration adherence report" in deviation_report.text
    assert "Exclude duplicate responses only." in deviation_report.text
    assert inventory_report.status_code == 200
    assert "Inventory report" in inventory_report.text
    assert "This is the published article." in inventory_report.text
