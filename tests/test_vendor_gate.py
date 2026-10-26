"""Tests for the vendor-readonly gate's wiring and behavior.

The gate protects vendor/spec/ and vendor/SPEC.md, which
mirror the contract owned by ezclassify/contracts and may only be written by
the spec bot. None of that matters if the workflow does not run, or runs
against an empty commit range.

Spec updates usually land by direct push, so a `pull_request`-only workflow
would never execute. The push-base fallback decides whether the range it
inspects is real: an inequality test lets `origin/main..HEAD` resolve empty
whenever HEAD is an ancestor of origin/main, and the gate would then exit 0
having read nothing. The range can also collapse to the tip alone; a tip that
touches nothing must then be refused, because the push only reaches this
workflow by touching a watched path, so an empty result over a collapsed range
means the change went unexamined.

Most assertions here are about wiring. They are not enough on their own: a
rewrite that keeps the words and loses the property passes every one of them.
So the identity and subject gates are also DRIVEN: the workflow's literal
`run:` body is extracted and executed by bash against a purpose-built git
history, and the exit codes are asserted.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/vendor-readonly.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")

BOT_NAME = "ezclassify-spec-bot[bot]"
BOT_EMAIL = "ezclassify-spec-bot[bot]@users.noreply.github.com"
CANONICAL_BOT_EMAIL = "348097708+ezclassify-spec-bot[bot]@users.noreply.github.com"
ZERO_SHA = "0" * 40


def _path_blocks() -> list[list[str]]:
    """Return the glob list under each trigger's `paths:` key."""
    blocks = re.findall(r"^ {4}paths:\n((?: {6}(?:#.*|- .*)\n)+)", WORKFLOW, re.M)
    return [
        [
            line.strip()[2:].strip("'")
            for line in block.splitlines()
            if line.strip().startswith("- ")
        ]
        for block in blocks
    ]


def test_gate_fires_on_push_not_only_on_pull_request() -> None:
    assert re.search(r"^  pull_request:$", WORKFLOW, re.M)
    assert re.search(r"^  push:$", WORKFLOW, re.M)


def test_both_triggers_watch_the_same_paths() -> None:
    blocks = _path_blocks()
    assert len(blocks) == 2, f"expected two paths blocks, found {len(blocks)}"
    assert blocks[0] == blocks[1]
    assert "vendor/spec/**" in blocks[0]
    assert "vendor/SPEC.md" in blocks[0]


def test_push_base_fallback_uses_reachability_not_string_inequality() -> None:
    """`!=` on SHAs is the bug that makes the fallback range resolve empty."""
    assert "git merge-base --is-ancestor" in WORKFLOW
    assert not re.search(r'\[ "\$PUSH_AFTER" != "\$DEFAULT_REF" \]', WORKFLOW)


def test_an_empty_range_is_narrowed_rather_than_passed() -> None:
    """A push that inspects nothing must never be reported as a clean pass."""
    assert "git rev-list --count $RANGE_ARGS" in WORKFLOW
    assert 'RANGE_ARGS="--no-walk $PUSH_AFTER"' in WORKFLOW
    # `-1 <sha> -- <paths>` does not mean "the tip commit": with a pathspec git
    # walks back to the most recent ancestor that touches it, so the run would
    # report a commit it did not claim to inspect. --no-walk is the real
    # single-commit form.
    assert 'RANGE_ARGS="-1 $PUSH_AFTER"' not in WORKFLOW


def test_recorded_push_base_is_accepted_only_when_it_is_an_ancestor() -> None:
    """`cat-file -e` alone is not enough to trust `github.event.before`.

    After a force-push the old tip is usually still an object in the clone, so
    existence passes while `before..after` quietly drops every commit the two
    branches share.
    """
    assert 'git merge-base --is-ancestor "$PUSH_BEFORE" "$PUSH_AFTER"' in WORKFLOW


def test_a_collapsed_range_that_finds_nothing_is_refused_not_passed() -> None:
    """A collapsed range that finds no vendor/ commit must refuse.

    `NARROWED_TO_TIP` is what distinguishes "the real range carried no vendor
    change" from "the commits that did are the ones I could not read". Without
    it the second case exits 0 having inspected nothing.
    """
    assert "NARROWED_TO_TIP=1" in WORKFLOW
    assert '[ "$NARROWED_TO_TIP" -eq 1 ]' in WORKFLOW
    refusal = WORKFLOW.split('[ "$NARROWED_TO_TIP" -eq 1 ]', 1)[1]
    # The refusal has to actually refuse, and before the clean exit.
    assert "exit 1" in refusal.split("exit 0", 1)[0]
    assert "this gate has checked NOTHING" in WORKFLOW


def test_the_clean_exit_says_which_range_it_inspected() -> None:
    """A pass has to state what it covered, or it reads the same as a miss."""
    assert "over the range this event introduced" in WORKFLOW


def test_a_narrowed_range_says_out_loud_what_it_did_not_inspect() -> None:
    assert "::warning::" in WORKFLOW
    assert "was NOT checked by this run" in WORKFLOW


def test_branch_creation_is_narrowed_not_hard_failed() -> None:
    """All-zeros `before` is an ordinary event, not an attack."""
    zeros = "ZERO=0000000000000000000000000000000000000000"
    assert zeros in WORKFLOW
    body = WORKFLOW.split(zeros, 1)[1]
    branch_creation = body.split("DEFAULT_REF=", 1)[0]
    assert "exit 1" not in branch_creation


def test_gate_is_not_disarmed() -> None:
    assert "continue-on-error" not in WORKFLOW
    assert "if: always()" not in WORKFLOW


def test_refusal_names_the_spec_bot_and_the_contracts_source() -> None:
    """A blocked contributor needs to know who writes the mirror and its source."""
    assert "the EzClassify spec bot" in WORKFLOW
    assert "ezclassify/contracts" in WORKFLOW


def test_identity_is_two_named_constants_not_a_pattern() -> None:
    """A reviewer must be able to read the admitted identity off the workflow.

    An arbitrary numeric prefix would admit another account. Both observed
    writer addresses are pinned explicitly.
    """
    assert f"ALLOWED_BOT_LOGIN: {BOT_NAME}" in WORKFLOW
    assert f"ALLOWED_BOT_EMAIL: {BOT_EMAIL}" in WORKFLOW
    assert f"ALLOWED_BOT_CANONICAL_EMAIL: {CANONICAL_BOT_EMAIL}" in WORKFLOW
    assert '[ "$AUTHOR" != "$ALLOWED_BOT_LOGIN <$ALLOWED_BOT_EMAIL>" ]' in WORKFLOW
    assert "([0-9]+\\+)?" not in WORKFLOW


# --------------------------------------------------------------------------
# Behavior: the workflow's own shell, executed.
# --------------------------------------------------------------------------


def _run_body() -> str:
    """Return the dedented shell of the workflow's single `run: |` block.

    Deliberately strict about the count. If the workflow ever grows a second
    `run:` step, this fails rather than silently driving whichever half it
    found first; a test that exercises the wrong half of a gate is worse than
    no test.
    """
    lines = WORKFLOW.split("\n")
    bodies: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        key_indent = len(line) - len(line.lstrip(" "))
        block: list[str] = []
        for following in lines[index + 1 :]:
            if not following.strip():
                block.append("")
                continue
            if len(following) - len(following.lstrip(" ")) <= key_indent:
                break
            block.append(following)
        widths = [len(b) - len(b.lstrip(" ")) for b in block if b]
        cut = min(widths) if widths else 0
        bodies.append("\n".join(b[cut:] for b in block))

    assert len(bodies) == 1, f"expected one `run: |` block, found {len(bodies)}"
    assert "set -euo pipefail" in bodies[0], "extracted block is not the gate"
    return bodies[0]


class _Repo:
    """A throwaway git repository the gate script is driven against."""

    def __init__(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def commit(self, name: str, email: str, subject: str, path: str, body: str) -> str:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        self.git("add", path)
        self.git(
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "-q",
            "-m",
            subject,
        )
        return self.git("rev-parse", "HEAD")


@pytest.fixture(scope="module")
def driven(tmp_path_factory: pytest.TempPathFactory):
    """Build the history and a runnable copy of the gate, once."""
    for tool in ("bash", "git"):
        # Not a skip. bash and git are required to build this repo at all, so a
        # missing one is a broken checkout, and skipping is how a gate goes
        # quietly inert.
        assert shutil.which(tool), f"{tool} is required to drive the vendor gate"

    root = tmp_path_factory.mktemp("vendor-gate")
    script = root / "gate.sh"
    script.write_text(_run_body(), encoding="utf-8")

    repo = _Repo(root / "repo")
    shas = {}
    shas["base"] = repo.commit(
        "Someone", "someone@example.com", "chore: seed", "README.md", "seed\n"
    )
    shas["bot"] = repo.commit(
        BOT_NAME,
        BOT_EMAIL,
        "chore(spec): vendor spec bundle from contracts@aaaaaaaa",
        "vendor/spec/MANIFEST.json",
        '{"one": true}\n',
    )
    # An unrecognized numeric account ID must still be refused.
    shas["numeric"] = repo.commit(
        BOT_NAME,
        f"12345+{BOT_EMAIL}",
        "chore(spec): vendor spec bundle from contracts@bbbbbbbb",
        "vendor/spec/MANIFEST.json",
        '{"two": true}\n',
    )
    # The bot's display name over somebody else's address.
    shas["wrong_email"] = repo.commit(
        BOT_NAME,
        "mallory@evil.example",
        "chore(spec): vendor spec bundle from contracts@cccccccc",
        "vendor/spec/MANIFEST.json",
        '{"three": true}\n',
    )
    shas["human"] = repo.commit(
        "Alex Maintainer",
        "alex.maintainer@example.com",
        "chore(spec): hand-edit the mirror",
        "vendor/SPEC.md",
        "hand edited\n",
    )
    shas["bad_subject"] = repo.commit(
        BOT_NAME,
        BOT_EMAIL,
        "feat: smuggle content through the spec channel",
        "vendor/spec/MANIFEST.json",
        '{"four": true}\n',
    )
    # The whole forgery is two `git config` lines plus a conventional subject.
    # See the test that pins this.
    shas["forged"] = repo.commit(
        BOT_NAME,
        BOT_EMAIL,
        "chore(spec): vendor spec bundle from contracts@aaaaaaaa",
        "vendor/spec/MANIFEST.json",
        '{"arbitrary": "payload"}\n',
    )
    repo.git("update-ref", "refs/remotes/origin/main", shas["forged"])
    return repo, script, shas


def _drive(
    repo, script, *, before, after, event="push", pr_author="", pr_labels=""
) -> tuple[int, str]:
    done = subprocess.run(
        ["bash", str(script)],
        cwd=repo.path,
        env={
            **repo.env,
            "EVENT_NAME": event,
            "PUSH_BEFORE": before if event == "push" else "",
            "PUSH_AFTER": after if event == "push" else "",
            "BASE_SHA": before if event != "push" else "",
            "HEAD_SHA": after if event != "push" else "",
            "DEFAULT_BRANCH": "main",
            "PR_AUTHOR": pr_author,
            "PR_LABELS": pr_labels,
            "ALLOWED_BOT_LOGIN": BOT_NAME,
            "ALLOWED_BOT_EMAIL": BOT_EMAIL,
            "ALLOWED_BOT_CANONICAL_EMAIL": CANONICAL_BOT_EMAIL,
            "OVERRIDE_LABEL": "spec-mirror-override",
        },
        capture_output=True,
        text=True,
    )
    return done.returncode, done.stdout + done.stderr


@pytest.mark.parametrize(
    "name,email,expected",
    [
        (BOT_NAME, CANONICAL_BOT_EMAIL, 0),
        (BOT_NAME, f"348097709+{BOT_EMAIL}", 1),
        (BOT_NAME, "348097708+other-bot[bot]@users.noreply.github.com", 1),
        ("Other Bot", CANONICAL_BOT_EMAIL, 1),
    ],
)
def test_github_squash_bot_identity_is_exact(driven, name, email, expected) -> None:
    """A GitHub squash author is admitted without accepting arbitrary ID prefixes."""
    repo, script, _ = driven
    before = repo.git("rev-parse", "HEAD")
    after = repo.commit(
        name,
        email,
        "chore(spec): vendor contracts@deadbeef (#9)",
        "vendor/spec/MANIFEST.json",
        email + name + "\n",
    )
    code, out = _drive(repo, script, before=before, after=after)
    assert code == expected, out
    if expected == 0:
        for author, wanted in [(BOT_NAME, 0), ("other-bot[bot]", 1)]:
            code, out = _drive(
                repo,
                script,
                before=before,
                after=after,
                event="pull_request",
                pr_author=author,
            )
            assert code == wanted, out


def test_the_real_bot_identity_is_admitted(driven) -> None:
    """An identity fix that blocks the writer breaks propagation silently."""
    repo, script, sha = driven
    code, out = _drive(repo, script, before=sha["base"], after=sha["bot"])
    assert code == 0, out
    assert sha["bot"] in out


@pytest.mark.parametrize(
    "case, expected_message",
    [
        ("numeric", "not authored by"),
        ("wrong_email", "not authored by"),
        ("human", "not authored by"),
        ("bad_subject", "disallowed subject"),
    ],
)
def test_gate_refuses(driven, case: str, expected_message: str) -> None:
    """Every identity that is not the writer's, and every foreign subject."""
    repo, script, sha = driven
    order = ["base", "bot", "numeric", "wrong_email", "human", "bad_subject", "forged"]
    before = order[order.index(case) - 1]
    code, out = _drive(repo, script, before=sha[before], after=sha[case])
    assert code == 1, out
    assert expected_message in out
    assert sha[case] in out


def test_the_gates_are_not_an_authentication_boundary(driven) -> None:
    """This ADMISSION is documented behavior, not a bug to fix here.

    Two lines of setup:

        git config user.name  "ezclassify-spec-bot[bot]"
        git config user.email "ezclassify-spec-bot[bot]@users.noreply.github.com"

    plus a `chore(spec):` subject produce a commit carrying any payload at
    all, and both gates admit it. `%an` and `%ae` are free-text object-header
    fields the writer picks and GitHub does not verify.

    The test exists so the workflow's header and this behavior cannot drift
    apart. The gates do not stop an attacker who can mimic both fields; they stop
    accidents, which the refusal tests above pin. Closing this for real means
    branch protection on the vendored paths or verified commit signatures, which
    are repository settings, not workflow edits.
    """
    repo, script, sha = driven
    code, out = _drive(repo, script, before=sha["bad_subject"], after=sha["forged"])
    assert code == 0, out
    assert "pass identity + subject gates" in out

    # And on the bot-PR path, where the GitHub-verified PR-author layer exists.
    code, out = _drive(
        repo,
        script,
        event="pull_request",
        before=sha["bad_subject"],
        after=sha["forged"],
        pr_author=BOT_NAME,
    )
    assert code == 0, out


def test_the_bot_pr_path_still_reads_every_commit(driven) -> None:
    """A PR the bot opened must not carry someone else's vendored commit."""
    repo, script, sha = driven
    code, out = _drive(
        repo,
        script,
        event="pull_request",
        before=sha["wrong_email"],
        after=sha["human"],
        pr_author=BOT_NAME,
    )
    assert code == 1, out
    assert sha["human"] in out
