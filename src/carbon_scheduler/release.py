"""Release tooling: version consistency checks, version bumps and release notes.

The single source of truth for the version is ``carbon_scheduler.__version__``.
``pyproject.toml`` reads it dynamically (hatch), and ``charts/carbon-scheduler/Chart.yaml``
keeps ``version`` and ``appVersion`` equal to it. ``check`` fails CI and the release
workflow when they drift, ``bump`` rewrites every location at once, and ``notes``
renders GitHub Release notes from Conventional Commits.

Usage::

    python -m carbon_scheduler.release check --tag v0.2.0
    python -m carbon_scheduler.release bump 0.2.0
    python -m carbon_scheduler.release notes --from v0.1.0 --to v0.2.0
"""

import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import click
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_FILE = Path("src/carbon_scheduler/__init__.py")
PYPROJECT_FILE = Path("pyproject.toml")
CHART_FILE = Path("charts/carbon-scheduler/Chart.yaml")

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_INIT_VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<description>.+)$"
)

# Section order in the rendered notes; types not listed here go to "Other changes".
SECTIONS: list[tuple[str, tuple[str, ...]]] = [
    ("Features", ("feat",)),
    ("Bug fixes", ("fix",)),
    ("Performance", ("perf",)),
    ("Documentation", ("docs",)),
    ("Maintenance", ("build", "ci", "chore", "refactor", "style", "test")),
]
_SKIPPED_SUBJECT_PREFIXES = ("Merge pull request", "Merge branch", "Merge remote-tracking")


class VersionMismatchError(Exception):
    """Raised when the version locations disagree."""


def read_versions(root: Path = REPO_ROOT) -> dict[str, str]:
    """Read every place the version lives.

    Args:
        root: Repository root.

    Returns:
        Mapping of location name to version string.

    Raises:
        VersionMismatchError: If a location is missing or unreadable.
    """
    init_text = (root / INIT_FILE).read_text()
    match = _INIT_VERSION_RE.search(init_text)
    if not match:
        raise VersionMismatchError(f"no __version__ found in {INIT_FILE}")
    versions = {"__version__": match.group(1)}

    pyproject = tomllib.loads((root / PYPROJECT_FILE).read_text())
    project = pyproject.get("project", {})
    if "version" in project:
        versions["pyproject.toml"] = str(project["version"])
    else:
        hatch_path = pyproject.get("tool", {}).get("hatch", {}).get("version", {}).get("path")
        if "version" not in project.get("dynamic", []) or hatch_path != str(INIT_FILE):
            raise VersionMismatchError(
                f"pyproject.toml must read its version dynamically from {INIT_FILE}"
            )

    chart = yaml.safe_load((root / CHART_FILE).read_text())
    versions["Chart.yaml version"] = str(chart.get("version"))
    versions["Chart.yaml appVersion"] = str(chart.get("appVersion"))
    return versions


def check_versions(root: Path = REPO_ROOT, tag: str | None = None) -> str:
    """Verify all version locations (and optionally a git tag) agree.

    Args:
        root: Repository root.
        tag: Release tag such as ``v0.2.0``; must equal ``v`` + the version.

    Returns:
        The agreed version.

    Raises:
        VersionMismatchError: If any location disagrees or the version is not SemVer.
    """
    versions = read_versions(root)
    version = versions["__version__"]
    if not SEMVER_RE.match(version):
        raise VersionMismatchError(f"__version__ {version!r} is not a SemVer version")
    drift = {name: value for name, value in versions.items() if value != version}
    if drift:
        details = ", ".join(f"{name}={value}" for name, value in drift.items())
        raise VersionMismatchError(f"version drift from __version__={version}: {details}")
    if tag is not None and tag != f"v{version}":
        raise VersionMismatchError(f"tag {tag!r} does not match version v{version}")
    return version


def bump_version(new_version: str, root: Path = REPO_ROOT) -> None:
    """Write a new version to ``__init__.py`` and ``Chart.yaml``.

    ``pyproject.toml`` needs no change because it reads ``__version__``.

    Args:
        new_version: SemVer version without a leading ``v``.
        root: Repository root.

    Raises:
        VersionMismatchError: If the version is not SemVer.
    """
    if not SEMVER_RE.match(new_version):
        raise VersionMismatchError(f"{new_version!r} is not a SemVer version")

    init_path = root / INIT_FILE
    init_path.write_text(
        _INIT_VERSION_RE.sub(f'__version__ = "{new_version}"', init_path.read_text())
    )

    # Rewrite line by line so comments and key order in Chart.yaml are preserved.
    chart_path = root / CHART_FILE
    chart_text = chart_path.read_text()
    chart_text = re.sub(r"^version: .*$", f"version: {new_version}", chart_text, flags=re.M)
    chart_text = re.sub(r"^appVersion: .*$", f'appVersion: "{new_version}"', chart_text, flags=re.M)
    chart_path.write_text(chart_text)


@dataclass(frozen=True)
class Commit:
    """A commit as read from ``git log``."""

    sha: str
    subject: str
    body: str = ""


@dataclass(frozen=True)
class ParsedCommit:
    """A commit classified by its Conventional Commits header."""

    sha: str
    type: str
    scope: str | None
    description: str
    breaking: bool


def parse_commit(commit: Commit) -> ParsedCommit | None:
    """Classify a commit by its Conventional Commits header.

    Args:
        commit: Commit to classify.

    Returns:
        The parsed commit, or ``None`` for merge commits. Non-conventional subjects
        get the type ``other``.
    """
    if commit.subject.startswith(_SKIPPED_SUBJECT_PREFIXES):
        return None
    breaking_footer = bool(re.search(r"^BREAKING[ -]CHANGE: ", commit.body, re.MULTILINE))
    match = _CONVENTIONAL_RE.match(commit.subject)
    if not match:
        return ParsedCommit(commit.sha, "other", None, commit.subject, breaking_footer)
    return ParsedCommit(
        sha=commit.sha,
        type=match.group("type").lower(),
        scope=match.group("scope"),
        description=match.group("description"),
        breaking=bool(match.group("breaking")) or breaking_footer,
    )


def render_release_notes(
    commits: list[Commit],
    version: str,
    previous_tag: str | None = None,
    repo_url: str = "https://github.com/mahmudul-islam32/carbon-aware-k8s-scheduler",
) -> str:
    """Render Markdown release notes grouped by Conventional Commit type.

    Args:
        commits: Commits in the release, newest first (``git log`` order).
        version: Version being released, without ``v``.
        previous_tag: Previous release tag, used for the compare link.
        repo_url: Repository URL for commit and compare links.

    Returns:
        Markdown release notes.
    """
    parsed = [p for p in (parse_commit(c) for c in commits) if p is not None]

    def line(p: ParsedCommit) -> str:
        scope = f"**{p.scope}:** " if p.scope else ""
        return f"- {scope}{p.description} ([`{p.sha[:7]}`]({repo_url}/commit/{p.sha}))"

    out = [f"## v{version}", ""]
    breaking = [p for p in parsed if p.breaking]
    if breaking:
        out += ["### ⚠️ Breaking changes", "", *(line(p) for p in breaking), ""]

    listed: set[str] = set()
    for title, types in SECTIONS:
        entries = [p for p in parsed if p.type in types]
        listed.update(types)
        if entries:
            out += [f"### {title}", "", *(line(p) for p in entries), ""]
    others = [p for p in parsed if p.type not in listed]
    if others:
        out += ["### Other changes", "", *(line(p) for p in others), ""]
    if not parsed:
        out += ["No changes.", ""]

    image = "ghcr.io/mahmudul-islam32/carbon-scheduler"
    out += [
        "### Install",
        "",
        "```bash",
        f"docker pull {image}:{version}",
        "helm install carbon-scheduler oci://ghcr.io/mahmudul-islam32/charts/carbon-scheduler "
        f"--version {version}",
        "```",
        "",
    ]
    if previous_tag:
        out += [f"**Full changelog:** {repo_url}/compare/{previous_tag}...v{version}", ""]
    return "\n".join(out)


def git_commits(rev_range: str, cwd: Path = REPO_ROOT) -> list[Commit]:
    """Read commits for a revision range from git.

    Args:
        rev_range: Range such as ``v0.1.0..v0.2.0`` or a single ref.
        cwd: Repository directory.

    Returns:
        Commits, newest first.
    """
    output = subprocess.run(
        ["git", "log", "--format=%H%x1f%s%x1f%b%x1e", rev_range],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    commits = []
    for record in output.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) == 3:
            commits.append(Commit(sha=fields[0], subject=fields[1], body=fields[2]))
    return commits


@click.group()
def main() -> None:
    """Release tooling for carbon-scheduler."""


@main.command()
@click.option("--tag", default=None, help="Release tag that must match the version (vX.Y.Z).")
def check(tag: str | None) -> None:
    """Fail when __version__, pyproject.toml and Chart.yaml disagree."""
    try:
        version = check_versions(tag=tag)
    except VersionMismatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(version)


@main.command()
@click.argument("version")
def bump(version: str) -> None:
    """Set VERSION in every location."""
    try:
        bump_version(version)
        check_versions()
    except VersionMismatchError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(f"version set to {version}")


@main.command()
@click.option("--from", "from_ref", default=None, help="Previous tag (exclusive).")
@click.option("--to", "to_ref", default="HEAD", show_default=True, help="Release ref.")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
def notes(from_ref: str | None, to_ref: str, output: Path | None) -> None:
    """Render release notes from Conventional Commits."""
    rev_range = f"{from_ref}..{to_ref}" if from_ref else to_ref
    text = render_release_notes(git_commits(rev_range), check_versions(), previous_tag=from_ref)
    if output:
        output.write_text(text)
    else:
        click.echo(text)


if __name__ == "__main__":
    main()
