"""Apple projects: recognizing one, and the report of a build the agent reads."""

from pathlib import Path

from forge.ports import AppleBuildResult, AppleIssue

MAX_ERRORS = 30
MAX_WARNINGS = 15
LOG_LINES = 40  # of the log's end, shown when a build fails


def is_apple_project(root: Path) -> bool:
    """True for a folder with an XcodeGen spec or an Xcode project or workspace at its top."""
    if (root / "project.yml").is_file():
        return True
    return any(root.glob("*.xcodeproj")) or any(root.glob("*.xcworkspace"))


def build_report(result: AppleBuildResult, root: Path) -> str:
    """Outcome, tests, errors and warnings (paths relative to the project), and on failure the
    end of the log."""
    outcome = "succeeded" if result.ok else "failed"
    lines = [f"{result.action} for {result.platform} (scheme {result.scheme}): {outcome}"]
    if result.action == "test":
        lines.append(f"tests: {result.tests_run} run, {result.tests_failed} failed")
    if result.artifact:
        lines.append(f"archive: {result.artifact}")
    errors = [i for i in result.issues if i.severity == "error"]
    warnings = [i for i in result.issues if i.severity == "warning"]
    lines += [issue_line(i, root) for i in errors[:MAX_ERRORS]]
    if len(errors) > MAX_ERRORS:
        lines.append(f"... and {len(errors) - MAX_ERRORS} more errors")
    lines += [issue_line(i, root) for i in warnings[:MAX_WARNINGS]]
    if len(warnings) > MAX_WARNINGS:
        lines.append(f"... and {len(warnings) - MAX_WARNINGS} more warnings")
    if not result.ok and result.log_tail:
        lines += ["", "end of the build log:", *result.log_tail.splitlines()[-LOG_LINES:]]
    return "\n".join(lines)


def issue_line(issue: AppleIssue, root: Path) -> str:
    """`file:line: severity: message`, the file relative to the project when it is inside."""
    if not issue.file:
        return f"{issue.severity}: {issue.message}"
    path = Path(issue.file)
    try:
        shown = path.relative_to(root).as_posix()
    except ValueError:
        shown = issue.file
    return f"{shown}:{issue.line}: {issue.severity}: {issue.message}"
