"""Fake-executable tests for the dispatch supervisor.

No real model is ever invoked: every scenario is a tiny Python script the
test writes into tmp_path. See docs/design/2026-08-15-dispatch-layer-design.md
§5 for the scenario table.

Run:  python3 -m pytest skill/tests/test_dispatch.py -q
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
SCRIPT = SKILL / "scripts" / "dispatch_agent.py"

HAPPY = """
print("verdict: PASS")
print("confidence: 0.9")
"""

PROSE_ONLY = """
print("looks good to me, no issues found")
"""

EXIT_THREE = """
import sys
print("partial work")
sys.exit(3)
"""

SILENT_OK = """
pass
"""


def write_fake(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


def run_dispatch(tmp_path, fake_argv, *, attempt_id="t1", deadline=30.0,
                 grace=1.0, schema="review", extra=(), harness_timeout=60):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", str(deadline),
         "--grace-seconds", str(grace),
         "--seat", "reviewer-1",
         "--output-schema", schema,
         *extra,
         "--", *map(str, fake_argv)],
        capture_output=True, text=True, timeout=harness_timeout)
    receipt_path = tmp_path / "receipts" / f"{attempt_id}.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    return proc, receipt


def test_happy_path_is_succeeded_with_a_digest(tmp_path):
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 0, proc.stderr
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["exit_status"] == 0
    assert receipt["result"]["schema_valid"] is True
    assert receipt["result"]["output_sha256"]
    assert receipt["timing"]["started_at"] and receipt["timing"]["finished_at"]
    stdout = Path(receipt["result"]["stdout_path"]).read_text()
    assert "verdict: PASS" in stdout


def test_nonzero_exit_is_failed(tmp_path):
    fake = write_fake(tmp_path, "boom.py", EXIT_THREE)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 1
    assert receipt["result"]["state"] == "FAILED"
    assert receipt["result"]["exit_status"] == 3


def test_exit_zero_with_empty_stdout_is_invalid_output(tmp_path):
    """An empty success is not a success — this is the "empty stdout looks
    like a failed review / silent skip" trap from the research docs."""
    fake = write_fake(tmp_path, "silent.py", SILENT_OK)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 6
    assert receipt["result"]["state"] == "INVALID_OUTPUT"
    assert receipt["result"]["schema_valid"] is False


def test_prose_without_a_verdict_is_invalid_output_under_review_schema(tmp_path):
    fake = write_fake(tmp_path, "prose.py", PROSE_ONLY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    assert proc.returncode == 6
    assert receipt["result"]["state"] == "INVALID_OUTPUT"


def test_schema_none_accepts_any_nonempty_output(tmp_path):
    fake = write_fake(tmp_path, "prose.py", PROSE_ONLY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake], schema="none")
    assert proc.returncode == 0
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_missing_binary_is_start_failed(tmp_path):
    proc, receipt = run_dispatch(tmp_path, ["/nonexistent/binary-xyz"])
    assert proc.returncode == 4
    assert receipt["result"]["state"] == "START_FAILED"
    assert receipt["result"]["exit_status"] is None


def test_prompt_file_feeds_stdin_and_is_hashed(tmp_path):
    reader = write_fake(tmp_path, "reader.py", """
    import sys
    data = sys.stdin.read()
    print(f"verdict: PASS")
    print(f"read {len(data)} bytes")
    """)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this diff please\n")
    proc, receipt = run_dispatch(tmp_path, [sys.executable, reader],
                                 extra=("--prompt-file", str(prompt)))
    assert proc.returncode == 0
    assert receipt["prompt_sha256"]
    assert "read 24 bytes" in Path(receipt["result"]["stdout_path"]).read_text()


def test_no_prompt_file_means_stdin_is_closed_not_waiting(tmp_path):
    """The grok->openai stdin-hang class (F-06): with no prompt file the
    child's stdin must be /dev/null, so a stdin read returns immediately
    instead of blocking forever."""
    reader = write_fake(tmp_path, "stdin_reader.py", """
    import sys
    data = sys.stdin.read()      # returns "" instantly if stdin is DEVNULL
    print("verdict: PASS")
    """)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, reader],
                                 harness_timeout=15)
    assert proc.returncode == 0
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_receipt_is_never_half_written(tmp_path):
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake])
    leftovers = list((tmp_path / "receipts").glob("*.tmp"))
    assert leftovers == []


def test_a_traversal_attempt_id_is_rejected_before_any_path_is_built(tmp_path):
    """attempt_id is interpolated directly into receipt/stdout/stderr paths;
    a `../` segment must never let it escape receipt_dir."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 attempt_id="../../escape")
    assert proc.returncode == 2
    assert receipt is None
    assert not (tmp_path / "receipts").exists()
    assert not (tmp_path.parent / "escape.json").exists()


def test_a_second_run_with_an_existing_attempt_id_is_refused(tmp_path):
    """Two attempts sharing an id must never clobber each other's receipt or
    output files. Exclusive attempt creation claims a SEPARATE sentinel file
    (<attempt_id>.claim) via O_CREAT|O_EXCL, never the receipt path itself
    — the receipt only ever appears via write_receipt's atomic replace of a
    complete JSON document. This test exercises the ordinary case: the
    first attempt has already completed and removed its own claim at its
    terminal write, so the second `run` claims fine, then finds the
    receipt already there, gives back the claim it just took, and refuses
    without touching the existing receipt."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc1, receipt1 = run_dispatch(tmp_path, [sys.executable, fake],
                                   attempt_id="dupe-1")
    assert proc1.returncode == 0
    original = (tmp_path / "receipts" / "dupe-1.json").read_text()
    # The completed attempt's claim sentinel is gone — its terminal write
    # already removed it.
    assert not (tmp_path / "receipts" / "dupe-1.claim").exists()

    proc2, _ = run_dispatch(tmp_path, [sys.executable, fake],
                            attempt_id="dupe-1")
    assert proc2.returncode == 2
    assert (tmp_path / "receipts" / "dupe-1.json").read_text() == original
    # The second attempt's own (momentary) claim must not linger either.
    assert not (tmp_path / "receipts" / "dupe-1.claim").exists()


def test_a_missing_prompt_file_is_refused_before_any_receipt_exists(tmp_path):
    """Every pre-spawn input is validated before the receipt is claimed: a
    missing --prompt-file must never leave a permanent STARTING receipt
    behind (status/cancel only know how to unwind RUNNING)."""
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 extra=("--prompt-file",
                                        str(tmp_path / "nonexistent.txt")))
    assert proc.returncode == 2
    assert receipt is None
    assert not (tmp_path / "receipts").exists()


def test_write_receipt_ignores_a_symlink_planted_at_the_old_predictable_tmp_name(
        tmp_path):
    """The vulnerability this closes: the round-2 tmp name was
    `<receipt>.<os.getpid()>.tmp` — guessable the instant a caller reads
    supervisor_pid off any receipt this same process already wrote.
    Sequence that reproduces it: write_receipt runs once (establishing this
    writer's own pid, exactly as a STARTING receipt would disclose it), an
    attacker plants a symlink at that now-known OLD predictable path
    pointing at an external file, then write_receipt runs again for the
    same attempt (standing in for the terminal write). mkstemp's
    unpredictable, O_CREAT|O_EXCL name means the second call never opens
    the planted path at all, so the external target is never touched and
    the planted symlink itself is left exactly as planted."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir(mode=0o700)
    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("do not touch")

    dispatch_agent.write_receipt(
        receipt_dir, {"attempt_id": "dupe-2", "result": {"state": "STARTING"}})

    old_predictable_tmp = receipt_dir / f"dupe-2.json.{os.getpid()}.tmp"
    old_predictable_tmp.symlink_to(outside_target)

    dispatch_agent.write_receipt(
        receipt_dir, {"attempt_id": "dupe-2", "result": {"state": "SUCCEEDED"}})

    assert outside_target.read_text() == "do not touch"
    assert os.path.islink(old_predictable_tmp)
    receipt = json.loads((receipt_dir / "dupe-2.json").read_text())
    assert receipt["result"]["state"] == "SUCCEEDED"


def test_output_open_failure_is_start_failed_with_no_leaked_fd_or_claim(tmp_path):
    """R3-3 moved the stdout/stderr `os.open(..., O_NOFOLLOW)` calls to AFTER
    the STARTING receipt exists and OUTSIDE the START_FAILED `OSError` arm
    that already covers a Popen failure — an ELOOP (a planted symlink), a
    permission failure, or ENOSPC there must terminate the same way: a
    terminal START_FAILED receipt, the claim released, and no leaked fd if
    the first open (stdout) succeeded before the second (stderr) raised. No
    path may leave a STARTING receipt behind. Planting a symlink at the
    stdout path makes O_NOFOLLOW raise ELOOP without needing real
    permission bits, and its target must stay untouched — O_NOFOLLOW
    refused the hop instead of opening (and truncating) through it."""
    fake = write_fake(tmp_path, "happy4.py", HAPPY)
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    outside_target = tmp_path / "outside2.txt"
    outside_target.write_text("do not touch")
    (receipts / "eloop1.stdout").symlink_to(outside_target)

    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 attempt_id="eloop1", schema="none")
    assert proc.returncode == 4, proc.stderr
    assert receipt["result"]["state"] == "START_FAILED"
    assert not (receipts / "eloop1.claim").exists()
    assert outside_target.read_text() == "do not touch"


def test_terminal_receipt_write_failure_is_best_effort_and_observable(
        tmp_path, monkeypatch, capsys):
    """DEFER-2: a failed terminal receipt write still leaves group cleanup
    and the attempt outcome intact. Persistence is best-effort: print
    `receipt write failed` on supervisor stderr, release the claim, do not
    turn the failure into crash exit 9, and leave any leftover tmp gone."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    real_write = dispatch_agent.write_receipt

    def _write(receipt_dir, receipt):
        if receipt["result"]["state"] not in ("STARTING", "RUNNING"):
            raise OSError("disk full")
        return real_write(receipt_dir, receipt)

    monkeypatch.setattr(dispatch_agent, "write_receipt", _write)

    fake = write_fake(tmp_path, "happy.py", HAPPY)
    receipt_dir = tmp_path / "receipts"
    rc = dispatch_agent.main([
        "run", "--attempt-id", "t1", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "reviewer-1", "--output-schema", "review",
        "--", sys.executable, str(fake)])
    captured = capsys.readouterr()
    assert "receipt write failed" in captured.err
    leftovers = list(receipt_dir.glob("*.tmp")) if receipt_dir.exists() else []
    assert leftovers == []
    assert not (receipt_dir / "t1.claim").exists()
    assert rc == 0
