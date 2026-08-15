"""Static contract tests over the skill's documents and config.

These exist because three research documents (docs/research/, 2026-08-15)
found the shipped documents making claims the artifact does not keep: every
documented invocation was cwd-dependent, templates omitted prompts, and the
observability contract listed fields nothing produces. Each test here pins
one of those contracts.

Run:  python3 -m pytest skill/tests/ -q
"""

import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from route_task import load_config  # noqa: E402

CFG = load_config()


# ---------------------------------------------------------------------------
# D1 — documented invocations must not depend on the caller's cwd
# (docs/design/2026-08-15-dispatch-layer-design.md, DD-1)
# ---------------------------------------------------------------------------

def test_docs_never_invoke_the_router_cwd_relative():
    """`python3 scripts/route_task.py` only works with cwd = skill root, which
    no caller is guaranteed — a background subagent inherits the project
    root. Worse, the failure exits 2, which the contract reserves for
    "invalid input". Every documented invocation carries the skill-root
    prefix instead."""
    for doc in (SKILL / "SKILL.md", SKILL / "references" / "examples.md"):
        text = doc.read_text()
        assert not re.search(r"python3 scripts/route_task\.py", text), doc.name
        assert '"$SKILL_DIR"/scripts/route_task.py' in text, doc.name
        assert re.search(r"^SKILL_DIR=", text, re.MULTILINE), doc.name


# ---------------------------------------------------------------------------
# F-06 / T2 — every bridge template must carry a quoted prompt, no shell
# hazards (DD-4)
# ---------------------------------------------------------------------------

def test_every_bridge_mechanism_carries_a_quoted_prompt_and_no_pipe():
    """`codex exec` with no prompt argument waits on stdin — a background
    shell with stdin open never reaches the model, and to the caller that is
    indistinguishable from an unresponsive model. An unquoted <prompt>
    word-splits, and a literal alternation inside a template is a shell pipe
    when pasted. Every `codex exec` mechanism must also carry a sandbox
    slot — DD-4's permission-pinning rule applies to all three hosts that
    bridge into openai, not just the ones written first."""
    for host, entries in CFG["transports"].items():
        for name, spec in entries.items():
            if name == "native":
                continue
            mech = spec["mechanism"]
            assert '"<prompt>"' in mech, (host, name, mech)
            assert "|" not in mech, (host, name, mech)
            if "codex exec" in mech:
                assert "-s <sandbox>" in mech, (host, name, mech)


# ---------------------------------------------------------------------------
# F-04 — the observability contract must not promise fields nothing produces
# (DD-3)
# ---------------------------------------------------------------------------

def test_routing_metrics_block_promises_only_what_the_router_emits():
    """`review_count` and `final_success` were listed under "Every route
    emits" with no producer anywhere in route_task.py. Pre-dispatch code
    cannot know either; they belong to the execution receipt."""
    text = (SKILL / "references" / "control-loop.md").read_text()
    block = re.search(r"routing_metrics:\n(.*?)```", text, re.S).group(1)
    for field in ("final_success", "review_count"):
        assert field not in block, field
        assert field in text, f"{field} must stay documented — as a receipt field"


# ---------------------------------------------------------------------------
# DD-5 / DD-8 — a seat that returns no verdict must be a defined outcome
# ---------------------------------------------------------------------------

def test_review_policy_defines_the_absent_reviewer():
    """The output contract admitted three verdicts and no absence, and the
    disagreement matrix was a 3x3 with no missing row — so the two natural
    moves (proceed on one verdict, or show it to a re-dispatched seat) were
    both the failure the independence rules exist to prevent."""
    text = (SKILL / "references" / "review-policy.md").read_text()
    assert "NO_RESPONSE" in text
    assert "### A seat that returns no verdict" in text
    # the matrix rows exist
    assert re.search(r"\|\s*any verdict\s*\|\s*`NO_RESPONSE`", text)
    assert re.search(r"\|\s*`NO_RESPONSE`\s*\|\s*`NO_RESPONSE`", text)
    # and the evidence-id source is stated
    assert "### Where the evidence id comes from" in text
    assert "attempt_id" in text
