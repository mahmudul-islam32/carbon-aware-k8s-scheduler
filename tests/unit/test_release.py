"""Unit tests for the release tooling (F-34)."""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from carbon_scheduler import __version__
from carbon_scheduler.release import (
    CHART_FILE,
    INIT_FILE,
    PYPROJECT_FILE,
    REPO_ROOT,
    Commit,
    VersionMismatchError,
    bump_version,
    check_versions,
    git_commits,
    parse_commit,
    read_versions,
    render_release_notes,
)

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Copy the files that hold the version into a scratch repository root."""
    for rel in (INIT_FILE, PYPROJECT_FILE, CHART_FILE):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, tmp_path / rel)
    return tmp_path


def _workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return data


def _steps(job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"][job]["steps"]
    return steps


class TestSingleVersionSource:
    """``__version__``, ``pyproject.toml`` and ``Chart.yaml`` stay in sync."""

    def test_repository_versions_agree(self) -> None:
        assert check_versions() == __version__

    def test_pyproject_reads_version_dynamically(self) -> None:
        versions = read_versions()
        assert "pyproject.toml" not in versions  # no second hard-coded copy
        assert versions["Chart.yaml version"] == __version__
        assert versions["Chart.yaml appVersion"] == __version__

    def test_chart_drift_is_detected(self, repo: Path) -> None:
        chart = repo / CHART_FILE
        chart.write_text(chart.read_text().replace(f"version: {__version__}", "version: 9.9.9"))
        with pytest.raises(VersionMismatchError, match="Chart.yaml version=9.9.9"):
            check_versions(repo)

    def test_hardcoded_pyproject_version_must_match(self, repo: Path) -> None:
        pyproject = repo / PYPROJECT_FILE
        pyproject.write_text(
            pyproject.read_text().replace('dynamic = ["version"]', 'version = "0.0.1"')
        )
        with pytest.raises(VersionMismatchError, match="pyproject.toml=0.0.1"):
            check_versions(repo)

    def test_tag_must_match_version(self) -> None:
        assert check_versions(tag=f"v{__version__}") == __version__
        with pytest.raises(VersionMismatchError, match="does not match"):
            check_versions(tag="v99.0.0")

    def test_bump_updates_every_location(self, repo: Path) -> None:
        bump_version("1.2.3-rc.1", repo)
        assert check_versions(repo, tag="v1.2.3-rc.1") == "1.2.3-rc.1"
        chart_text = (repo / CHART_FILE).read_text()
        assert 'appVersion: "1.2.3-rc.1"' in chart_text
        assert "kubeVersion:" in chart_text  # other keys untouched

    def test_bump_rejects_non_semver(self, repo: Path) -> None:
        with pytest.raises(VersionMismatchError, match="SemVer"):
            bump_version("v1.2", repo)


class TestReleaseNotes:
    """GitHub Release notes are generated from Conventional Commits."""

    def test_parse_conventional_header(self) -> None:
        parsed = parse_commit(Commit("a" * 40, "feat(controller): relative deadlines (F-22)"))
        assert parsed is not None
        assert (parsed.type, parsed.scope, parsed.breaking) == ("feat", "controller", False)
        assert parsed.description == "relative deadlines (F-22)"

    def test_breaking_marker_and_footer(self) -> None:
        bang = parse_commit(Commit("b" * 40, "feat!: drop v1 API"))
        footer = parse_commit(Commit("c" * 40, "fix: rename key", "BREAKING CHANGE: key renamed"))
        assert bang is not None and bang.breaking
        assert footer is not None and footer.breaking

    def test_merge_commits_skipped_and_others_kept(self) -> None:
        assert parse_commit(Commit("d" * 40, "Merge pull request #2 from x/y")) is None
        other = parse_commit(Commit("e" * 40, "Initial project documentation"))
        assert other is not None and other.type == "other"

    def test_render_groups_by_type(self) -> None:
        commits = [
            Commit("1" * 40, "ci: release workflow"),
            Commit("2" * 40, "Merge pull request #3 from a/b"),
            Commit("3" * 40, "fix(grid): handle 429"),
            Commit("4" * 40, "feat(controller): re-plan on refresh"),
            Commit("5" * 40, "feat!: new annotation prefix"),
        ]
        notes = render_release_notes(commits, "0.2.0", previous_tag="v0.1.0")
        assert notes.startswith("## v0.2.0")
        sections = [line for line in notes.splitlines() if line.startswith("### ")]
        assert sections == [
            "### ⚠️ Breaking changes",
            "### Features",
            "### Bug fixes",
            "### Maintenance",
            "### Install",
        ]
        assert "- **controller:** re-plan on refresh ([`4444444`]" in notes
        assert "Merge pull request" not in notes
        assert "compare/v0.1.0...v0.2.0" in notes
        assert "carbon-scheduler --version 0.2.0" in notes

    def test_render_empty_release(self) -> None:
        assert "No changes." in render_release_notes([], "0.2.0")

    def test_git_commits_reads_log(self, tmp_path: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
            )

        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "feat: one")
        git("commit", "-q", "--allow-empty", "-m", "fix: two", "-m", "BREAKING CHANGE: yes")
        commits = git_commits("HEAD", cwd=tmp_path)
        assert [c.subject for c in commits] == ["fix: two", "feat: one"]
        assert "BREAKING CHANGE: yes" in commits[0].body


class TestReleaseWorkflow:
    """The tag-triggered workflow publishes image, chart and release."""

    def test_triggered_by_version_tags(self) -> None:
        # PyYAML reads the bare key `on` as boolean True.
        assert _workflow()[True] == {"push": {"tags": ["v*"]}}

    def test_multi_arch_image_pushed_to_ghcr(self) -> None:
        workflow = _workflow()
        assert workflow["env"]["IMAGE"] == "ghcr.io/mahmudul-islam32/carbon-scheduler"
        assert workflow["jobs"]["image"]["permissions"]["packages"] == "write"
        build = next(s for s in _steps("image") if s.get("name") == "Build and push")
        assert build["with"]["push"] is True
        assert set(build["with"]["platforms"].split(",")) == {"linux/amd64", "linux/arm64"}

    def test_chart_published_as_oci(self) -> None:
        assert _workflow()["env"]["CHART_REPO"] == "oci://ghcr.io/mahmudul-islam32/charts"
        runs = " ".join(s.get("run", "") for s in _steps("chart"))
        assert "helm package charts/carbon-scheduler" in runs
        assert 'helm push dist/carbon-scheduler-${{ needs.verify.outputs.version }}.tgz "$CHART_REPO"' in runs

    def test_publishing_waits_for_version_check(self) -> None:
        jobs = _workflow()["jobs"]
        check = next(s for s in _steps("verify") if s.get("id") == "version")
        assert 'release check --tag "$GITHUB_REF_NAME"' in check["run"]
        assert jobs["image"]["needs"] == "verify"
        assert jobs["chart"]["needs"] == "verify"
        assert set(jobs["github-release"]["needs"]) == {"verify", "image", "chart"}

    def test_release_uses_generated_notes(self) -> None:
        runs = " ".join(s.get("run", "") for s in _steps("github-release"))
        assert "carbon_scheduler.release notes" in runs
        assert "gh release create" in runs and "--notes-file notes.md" in runs
