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

SLEEPER = """
import time
time.sleep(60)
"""

TERM_IGNORER = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
"""

LATE_WRITER = """
import signal, sys, time
def bail(*_):
    print("verdict: PASS")
    sys.stdout.flush()
    sys.exit(0)
signal.signal(signal.SIGTERM, bail)
time.sleep(60)
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


def test_deadline_expiry_is_timed_out_and_confirmed(tmp_path):
    """Without a deadline the supervisor inherits the research docs' core
    finding: an unresponsive model waits forever. The harness timeout is the
    RED phase here — an unimplemented deadline hangs this test."""
    fake = write_fake(tmp_path, "sleeper.py", SLEEPER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=1.0, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["termination_confirmed"] is True
    assert receipt["timing"]["finished_at"]


def test_term_ignorer_is_killed_and_still_timed_out(tmp_path):
    fake = write_fake(tmp_path, "stubborn.py", TERM_IGNORER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=0.5, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["termination_confirmed"] is True


def test_a_verdict_written_after_the_deadline_stays_timed_out(tmp_path):
    """The invariant from DD-9: once the deadline fired, no output can
    produce SUCCEEDED. A partial dump after the kill is not a review."""
    fake = write_fake(tmp_path, "late.py", LATE_WRITER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=2.0, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["schema_valid"] is None
    # the late output exists on disk — and was still not graded
    assert "verdict: PASS" in Path(receipt["result"]["stdout_path"]).read_text()


def test_a_post_spawn_crash_still_confirms_termination_and_writes_a_terminal_receipt(
        tmp_path, monkeypatch):
    """Task 8's `cmd_run` wraps everything after Popen succeeds in
    try/except Exception precisely so a crash there cannot abandon a live
    process group behind a receipt stuck at RUNNING. This is the one
    in-process test in this file: it needs to monkeypatch a name inside the
    module before calling `main()`, which the subprocess-driven harness the
    rest of this file uses cannot do."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch_agent, "_validate_output", _boom)
    fake = write_fake(tmp_path, "happy.py", HAPPY)
    receipt_dir = tmp_path / "receipts"
    rc = dispatch_agent.main([
        "run", "--attempt-id", "crash1", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "review",
        "--", sys.executable, str(fake)])
    assert rc == 9
    receipt = json.loads((receipt_dir / "crash1.json").read_text())
    assert receipt["result"]["state"] == "CANCELLED"
    assert receipt["result"]["termination_confirmed"] is True
    assert receipt["timing"]["finished_at"]
    pgid = receipt["process"]["process_group_id"]
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


GRANDCHILD_SPAWNER_TIMEOUT = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
"""

ORPHAN_LEAVER = """
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print("verdict: PASS")
"""

FLOODER = """
import sys
chunk = "x" * 65536
for _ in range(160):          # ~10 MB — enough to jam any pipe buffer
    sys.stdout.write(chunk)
sys.stdout.write("\\nverdict: PASS\\n")
"""


def _group_is_dead(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True


def test_timeout_kills_the_grandchild_too(tmp_path):
    """F-02: killing only the leader leaves a live writer behind — the
    duplicate-writer scenario. The whole group must be confirmed dead."""
    fake = write_fake(tmp_path, "spawner.py", GRANDCHILD_SPAWNER_TIMEOUT)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=1.0, grace=2.0, harness_timeout=30)
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["termination_confirmed"] is True
    assert _group_is_dead(receipt["process"]["process_group_id"])


def test_a_clean_exit_that_leaves_an_orphan_is_cleaned_and_confirmed(tmp_path):
    """The leader exiting 0 is not the end of the attempt: an orphaned
    grandchild is still our dispatch. It is reaped before the result is
    called a result."""
    fake = write_fake(tmp_path, "orphan.py", ORPHAN_LEAVER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 grace=2.0, harness_timeout=30)
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert receipt["result"]["termination_confirmed"] is True
    assert _group_is_dead(receipt["process"]["process_group_id"])


def test_a_flooding_child_cannot_deadlock_the_supervisor(tmp_path):
    """stdout goes to a file, not a pipe — 10 MB must complete, not jam."""
    fake = write_fake(tmp_path, "flooder.py", FLOODER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=30.0, harness_timeout=60)
    assert receipt["result"]["state"] == "SUCCEEDED"
    assert Path(receipt["result"]["stdout_path"]).stat().st_size > 10_000_000


GRANDCHILD_TERM_IGNORER_QUICK_LEADER = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
time.sleep(0.3)
print("verdict: PASS")
"""


def test_grading_is_gated_on_the_deadline_even_after_the_leader_exits_0(tmp_path):
    """DD-9's invariant ("no output can produce SUCCEEDED once the deadline
    has expired") is about the moment GRADING happens, not merely the
    moment the leader exited. Here the leader exits 0 well inside the
    deadline, but its TERM-ignoring grandchild forces the normal-exit
    branch's group-confirmation cleanup to run past the deadline before
    grading is ever reached. The gate sits between confirmation and
    grading: past the deadline, the state is TIMED_OUT regardless of the
    leader's exit status, exit_status is still recorded, and schema_valid
    stays null — a post-deadline result is never graded."""
    fake = write_fake(tmp_path, "quick_leader_stuck_grandchild.py",
                      GRANDCHILD_TERM_IGNORER_QUICK_LEADER)
    proc, receipt = run_dispatch(tmp_path, [sys.executable, fake],
                                 deadline=0.5, grace=1.0, harness_timeout=30)
    assert proc.returncode == 3
    assert receipt["result"]["state"] == "TIMED_OUT"
    assert receipt["result"]["exit_status"] == 0
    assert receipt["result"]["schema_valid"] is None
    assert receipt["result"]["termination_confirmed"] is True


def _start_supervised_sleeper(tmp_path, attempt_id="bg1"):
    fake = write_fake(tmp_path, "sleeper_bg.py", SLEEPER)
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", "60", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "none",
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = tmp_path / "receipts" / f"{attempt_id}.json"
    for _ in range(100):                       # wait for RUNNING, <=5 s
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["result"]["state"] == "RUNNING":
                return supervisor, receipt
        time.sleep(0.05)
    supervisor.kill()
    pytest.fail("supervisor never reached RUNNING")


def _agent(args, tmp_path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args,
         "--receipt-dir", str(tmp_path / "receipts")],
        capture_output=True, text=True, timeout=30)


def test_status_reports_running_with_liveness(tmp_path):
    supervisor, _ = _start_supervised_sleeper(tmp_path)
    try:
        proc = _agent(["status", "--attempt-id", "bg1"], tmp_path)
        assert proc.returncode == 0
        shown = json.loads(proc.stdout)
        assert shown["result"]["state"] == "RUNNING"
        assert shown["process_alive"] is True
        assert shown["supervision"] == "supervised"
    finally:
        _agent(["cancel", "--attempt-id", "bg1"], tmp_path)
        supervisor.wait(timeout=30)


def test_status_detects_an_orphaned_child_when_the_supervisor_died(tmp_path):
    """The receipt's own supervisor_pid is what makes this detectable: if the
    `run` process dies while the child lives on, process_alive alone (which
    only checks the child's group) would report a plain RUNNING attempt as
    if someone were still watching its deadline. `status` must say
    'orphaned' instead — a stale/orphaned RUNNING receipt requires cancel
    before any retry (F-02: a retry behind a possibly-live writer is two
    writers on the same files)."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg5")
    try:
        path = tmp_path / "receipts" / "bg5.json"
        on_disk = json.loads(path.read_text())
        dead_pid = 999999  # not our process — the group's own child is
        # still alive, so this cannot collide with a real pid this test uses
        on_disk["process"]["supervisor_pid"] = dead_pid
        path.write_text(json.dumps(on_disk))
        proc = _agent(["status", "--attempt-id", "bg5"], tmp_path)
        assert proc.returncode == 0
        shown = json.loads(proc.stdout)
        assert shown["process_alive"] is True
        assert shown["supervision"] == "orphaned"
    finally:
        # `cancel` cannot clean this up: the on-disk supervisor_pid above
        # was overwritten to a dead pid to simulate the orphaned case, so
        # cancel correctly refuses to signal (the stale-refusal rule this
        # test's sibling exercises directly) — using it here would leave
        # the real 60s sleeper outliving supervisor.wait below. Kill the
        # recorded process group directly instead.
        pgid = receipt["process"]["process_group_id"]
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        supervisor.wait(timeout=30)


def test_cancel_confirms_and_the_run_supervisor_preserves_it(tmp_path):
    """cancel and run race on the receipt; the cancel verdict wins — the
    attempt WAS killed, whatever the child's exit looked like to run."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg2")
    proc = _agent(["cancel", "--attempt-id", "bg2", "--grace-seconds", "2"],
                  tmp_path)
    assert proc.returncode == 7, proc.stderr
    supervisor.wait(timeout=30)
    final = json.loads((tmp_path / "receipts" / "bg2.json").read_text())
    assert final["result"]["state"] == "CANCELLED"
    assert final["result"]["termination_confirmed"] is True
    assert _group_is_dead(receipt["process"]["process_group_id"])


def test_cancel_of_a_finished_attempt_is_refused(tmp_path):
    fake = write_fake(tmp_path, "happy2.py", HAPPY)
    run_dispatch(tmp_path, [sys.executable, fake], attempt_id="done1")
    proc = _agent(["cancel", "--attempt-id", "done1"], tmp_path)
    assert proc.returncode == 0            # already SUCCEEDED — nothing to kill
    assert "not RUNNING" in proc.stderr


def test_cancel_refuses_to_signal_when_the_recorded_supervisor_is_dead(tmp_path):
    """A stale receipt's recorded pgid may have been reused by an unrelated
    process once the supervisor that watched it is gone — cancel must not
    kill blind. POSIX has no portable check that a pgid still identifies
    the same process group, so a dead supervisor means refuse, not
    signal."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg6")
    pgid = receipt["process"]["process_group_id"]
    try:
        path = tmp_path / "receipts" / "bg6.json"
        on_disk = json.loads(path.read_text())
        dead_pid = 999999  # not our process — the child group is still
        # alive, so this cannot collide with a real pid this test uses
        on_disk["process"]["supervisor_pid"] = dead_pid
        path.write_text(json.dumps(on_disk))

        proc = _agent(["cancel", "--attempt-id", "bg6"], tmp_path)
        assert proc.returncode == 5, proc.stderr
        final = json.loads(path.read_text())
        assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
        assert final["result"]["termination_confirmed"] is False
        # cancel must not have touched the group — it is still alive
        assert not _group_is_dead(pgid)
    finally:
        os.killpg(pgid, signal.SIGKILL)  # test cleanup, not the code under test
        supervisor.wait(timeout=30)


def test_cancel_of_a_stale_starting_receipt_is_terminal_with_no_signal(tmp_path):
    """A STARTING receipt whose supervisor died before ever reaching RUNNING
    is stale exactly like a stale RUNNING receipt — there is no live
    supervisor to trust a pgid's identity against, and a STARTING receipt
    may not even have a pgid yet. cancel must still resolve it to a
    terminal state (so a caller is not stuck polling a receipt nothing will
    ever finish) without sending any signal — there is nothing recorded
    that it could safely signal."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    dead_pid = 999999  # not our process
    starting = {
        "attempt_id": "stale-starting", "seat": "worker",
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": dead_pid},
        "timing": {"started_at": None, "deadline_at": None, "finished_at": None},
        "result": {"state": "STARTING", "exit_status": None,
                  "stdout_path": None, "stderr_path": None,
                  "output_sha256": None, "schema_valid": None,
                  "termination_confirmed": None},
    }
    (receipts / "stale-starting.json").write_text(json.dumps(starting))
    proc = _agent(["cancel", "--attempt-id", "stale-starting"], tmp_path)
    assert proc.returncode == 5, proc.stderr
    final = json.loads((receipts / "stale-starting.json").read_text())
    assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
    assert final["result"]["termination_confirmed"] is False


def test_cancel_of_a_live_starting_receipt_refuses_without_a_signal(tmp_path):
    """A STARTING receipt whose supervisor is alive but has not yet reached
    RUNNING has no child process group to signal — cancel refuses instead
    of guessing, and must leave the receipt exactly as it found it (simulate
    the live supervisor with this test process's own pid, since it is
    guaranteed alive for the duration of the call)."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    starting = {
        "attempt_id": "live-starting", "seat": "worker",
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": os.getpid()},
        "timing": {"started_at": None, "deadline_at": None, "finished_at": None},
        "result": {"state": "STARTING", "exit_status": None,
                  "stdout_path": None, "stderr_path": None,
                  "output_sha256": None, "schema_valid": None,
                  "termination_confirmed": None},
    }
    payload = json.dumps(starting)
    (receipts / "live-starting.json").write_text(payload)
    proc = _agent(["cancel", "--attempt-id", "live-starting"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "not yet RUNNING" in proc.stderr
    assert (receipts / "live-starting.json").read_text() == payload


def test_status_reports_a_claim_with_no_receipt_yet_as_claimed(tmp_path):
    """CLAIMED is a report-only label for the window between a successful
    O_CREAT|O_EXCL claim (Task 8) and the first receipt write — normally too
    narrow to observe by racing a real `run`, so simulated directly here by
    writing only the sentinel file. It must never be confused with a
    receipt state: there is no receipt to read one out of yet."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "claimed-only.claim").write_text("")
    proc = _agent(["status", "--attempt-id", "claimed-only"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert shown["state"] == "CLAIMED"


def test_run_preserves_a_termination_unconfirmed_receipt_too(tmp_path):
    """The preservation check must not stop at CANCELLED: a cancel whose own
    kill ladder could not confirm the group dead writes
    TERMINATION_UNCONFIRMED, and that verdict is just as authoritative — it
    is the one state that must block a write-capable retry (DD-10), so
    `run` must never overwrite it with its own conclusion (typically
    TIMED_OUT once the signal actually lands). Direct-state simulation:
    writing TERMINATION_UNCONFIRMED onto the on-disk receipt stands in for
    an external cancel process reaching that same verdict — the
    preservation check in `cmd_run` reads whatever state is on disk, not
    who wrote it."""
    fake = write_fake(tmp_path, "sleeper_bg2.py", SLEEPER)
    attempt_id = "bg4"
    supervisor = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run",
         "--attempt-id", attempt_id,
         "--receipt-dir", str(tmp_path / "receipts"),
         "--deadline-seconds", "1", "--grace-seconds", "1",
         "--seat", "worker", "--output-schema", "none",
         "--", sys.executable, str(fake)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt_path = tmp_path / "receipts" / f"{attempt_id}.json"
    for _ in range(100):                       # wait for RUNNING, <=5 s
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["result"]["state"] == "RUNNING":
                break
        time.sleep(0.05)
    else:
        supervisor.kill()
        pytest.fail("supervisor never reached RUNNING")
    on_disk = json.loads(receipt_path.read_text())
    on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
    on_disk["result"]["termination_confirmed"] = False
    receipt_path.write_text(json.dumps(on_disk))
    supervisor.wait(timeout=30)
    final = json.loads(receipt_path.read_text())
    assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"


def test_a_post_spawn_crash_never_relabels_an_already_terminal_receipt(
        tmp_path, monkeypatch):
    """Task 8's post-spawn `except Exception` handler used to write its own
    conclusion (CANCELLED/TERMINATION_UNCONFIRMED) unconditionally — if an
    external `cancel` had already landed a terminal state for this same
    attempt in the narrow window before the crash handler's own write, the
    crash handler's write would relabel it: exactly the relabeling hazard
    design doc DD-9 documents for the run/cancel race, now reachable from
    the crash path too (ITEM-V-4). The fix: both the normal tail (the test
    above) and this crash handler now go through the shared
    `_commit_terminal` helper, so whichever terminal state is on disk right
    before the final atomic write wins, no matter which of the two code
    paths gets there last. Direct-state simulation stands in for an actual
    concurrent `cancel`, exactly as the test above does for the normal
    tail — the preservation check reads whatever state is on disk, not who
    wrote it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dispatch_agent", SCRIPT)
    dispatch_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch_agent)

    receipt_dir = tmp_path / "receipts"

    def _plant_termination_unconfirmed_then_boom(stdout_path, output_schema):
        on_disk = json.loads((receipt_dir / "crash3.json").read_text())
        on_disk["result"]["state"] = "TERMINATION_UNCONFIRMED"
        on_disk["result"]["termination_confirmed"] = False
        (receipt_dir / "crash3.json").write_text(json.dumps(on_disk))
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch_agent, "_validate_output",
                        _plant_termination_unconfirmed_then_boom)
    fake = write_fake(tmp_path, "happy3.py", HAPPY)
    rc = dispatch_agent.main([
        "run", "--attempt-id", "crash3", "--receipt-dir", str(receipt_dir),
        "--deadline-seconds", "30", "--grace-seconds", "1",
        "--seat", "worker", "--output-schema", "review",
        "--", sys.executable, str(fake)])
    assert rc == 9
    receipt = json.loads((receipt_dir / "crash3.json").read_text())
    # The crash handler's own conclusion would have been CANCELLED (the
    # group WAS confirmed dead — the child had already exited 0 before
    # _validate_output ever ran) — the pre-planted TERMINATION_UNCONFIRMED
    # must win instead.
    assert receipt["result"]["state"] == "TERMINATION_UNCONFIRMED"
    assert receipt["result"]["termination_confirmed"] is False
    assert not (receipt_dir / "crash3.claim").exists()


def test_cancel_of_a_claim_only_attempt_refuses_without_touching_the_claim(tmp_path):
    """A claim sentinel with no receipt yet (Task 8's narrow claim-then-
    STARTING window, or a supervisor that crashed inside it) must not crash
    `cancel` — `read_receipt` would raise `FileNotFoundError` on a bare
    attempt id with only a claim (ITEM-V-5). There is nothing recorded to
    signal yet (no pgid, no supervisor_pid to check liveness against), so
    `cancel` refuses without touching the claim; the documented remedy
    (confirm the claimer is dead, then delete the claim manually) stays
    manual — no automatic claim garbage collection."""
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "claim-only-1.claim").write_text("")
    proc = _agent(["cancel", "--attempt-id", "claim-only-1"], tmp_path)
    assert proc.returncode == 2, proc.stderr
    assert "claimed but never started" in proc.stderr
    assert (receipts / "claim-only-1.claim").exists()
    assert not (receipts / "claim-only-1.json").exists()


def test_cancel_of_a_stale_running_receipt_removes_the_claim_too(tmp_path):
    """`cancel`'s stale-supervisor branch writes a terminal
    TERMINATION_UNCONFIRMED receipt — the rule that any command writing a
    terminal receipt also removes the claim (ITEM-V-5) applies here exactly
    as it does to `run`'s own terminal writes. Leaving the claim behind
    would contradict `status`'s CLAIMED report: a claim with no attempt
    behind it, when a terminal receipt in fact already exists."""
    supervisor, receipt = _start_supervised_sleeper(tmp_path, attempt_id="bg7")
    pgid = receipt["process"]["process_group_id"]
    try:
        path = tmp_path / "receipts" / "bg7.json"
        claim_path = tmp_path / "receipts" / "bg7.claim"
        assert claim_path.exists()          # still RUNNING — not yet released
        on_disk = json.loads(path.read_text())
        dead_pid = 999999  # not our process — the child group is still
        # alive, so this cannot collide with a real pid this test uses
        on_disk["process"]["supervisor_pid"] = dead_pid
        path.write_text(json.dumps(on_disk))

        proc = _agent(["cancel", "--attempt-id", "bg7"], tmp_path)
        assert proc.returncode == 5, proc.stderr
        final = json.loads(path.read_text())
        assert final["result"]["state"] == "TERMINATION_UNCONFIRMED"
        assert not claim_path.exists()
    finally:
        os.killpg(pgid, signal.SIGKILL)  # test cleanup, not the code under test
        supervisor.wait(timeout=30)

