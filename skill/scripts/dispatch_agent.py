#!/usr/bin/env python3
"""Bounded execution supervisor for one background dispatch attempt.

`route_task.py` decides who runs. This script owns the time axis after that
decision — the part of the Layer B contract `references/adapters.md` claims:
start confirmation, a wall-clock deadline, cancellation with escalation
(TERM, a grace period, then KILL against the whole process group),
termination confirmation, and a completion receipt. The receipt is
completion evidence; it is deliberately distinct from any isolation claim
passed to the router as `--isolation-evidence`, and neither substitutes for
the other.

Design rules this file enforces:

- A spawn handle is not a result. The receipt reaches a terminal state only
  when the process group is confirmed finished.
- Once the deadline has expired, no output can produce SUCCEEDED. A verdict
  written during the grace period stays TIMED_OUT — a late fragment is not
  a review.
- If the process group cannot be confirmed dead, the terminal state is
  TERMINATION_UNCONFIRMED, and no write-capable retry may follow it
  (`route_task.py --flags termination_unconfirmed` holds the route).
- A crash anywhere after Popen succeeds — writing the RUNNING receipt,
  waiting on the child, validating output, or writing the terminal receipt
  itself — still runs the termination ladder and leaves a terminal receipt
  (CANCELLED if the group's death is confirmed, TERMINATION_UNCONFIRMED if
  not) before the crash is re-raised as exit 9. A supervisor crash must
  never be a silent abandonment of a live process group behind a receipt
  stuck at RUNNING.
- Prompts travel by file into the child's stdin; with no prompt file, stdin
  is /dev/null, so waiting on stdin is structurally impossible. argv is
  executed without a shell.
- `--attempt-id` must match a safe identifier grammar before any path is
  built from it, and every path derived from it must resolve inside
  `--receipt-dir` — no `../` escape; every subcommand (`run`, `status`,
  `cancel`, `verify-evidence`) validates through the same chokepoint before
  its first path, not just `run`. Exclusive attempt creation claims a
  separate sentinel file (`<attempt_id>.claim`), never the receipt path
  itself: the receipt file only ever appears via an atomic replace of a
  complete JSON document, so it is never observable half-written OR empty.
  `run` refuses to start a second attempt under an attempt-id whose claim
  or receipt already exists; it never overwrites a live attempt's receipt
  or output files.

Subcommands: run | status | cancel | verify-evidence

Exit status: 0 SUCCEEDED; 1 FAILED; 2 invalid usage; 3 TIMED_OUT;
4 START_FAILED; 5 TERMINATION_UNCONFIRMED; 6 INVALID_OUTPUT;
7 CANCELLED (cancel: confirmed); 9 internal error — a crash, never an
attempt outcome (the same lesson route_task.py's exit 5 encodes). status
exits 0 and prints the receipt. verify-evidence exits 0 iff the evidence
set is exactly valid.

POSIX only: process-group control uses start_new_session and os.killpg.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

STATES = (
    "STARTING", "RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT",
    "CANCELLED", "START_FAILED", "TERMINATION_UNCONFIRMED", "INVALID_OUTPUT",
)
# CLAIMED is deliberately NOT a member: it is `status`'s report-only label
# for "a claim sentinel exists but no receipt does yet" (see cmd_status) —
# there is no receipt in that window to hold a `result.state`, so it is not
# a receipt state and never appears in a receipt's `result.state` field.

EXIT_BY_STATE = {
    "SUCCEEDED": 0, "FAILED": 1, "TIMED_OUT": 3, "START_FAILED": 4,
    "TERMINATION_UNCONFIRMED": 5, "INVALID_OUTPUT": 6, "CANCELLED": 7,
}

VERDICT_RE = re.compile(r"^verdict:\s*(PASS|PASS_WITH_CHANGES|FAIL)\b", re.M)

# Safe identifier grammar for attempt-id: this string is interpolated
# directly into filesystem paths, so it must never contain a path
# separator or a traversal segment.
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validated_attempt_id(value: str) -> str:
    """The single validation chokepoint every subcommand passes through
    before any path is built from a caller-supplied attempt id — `run`,
    `status`, `cancel`, and `verify-evidence` all call this before their
    first `_receipt_path`/`read_receipt`, not just `run`. Raises ValueError
    (never lets an id past the grammar reach a path); every caller maps
    that the same way, to exit 2 with a usage message, before touching the
    filesystem."""
    if not ATTEMPT_ID_RE.match(value):
        raise ValueError(
            f"invalid attempt-id {value!r}: must match {ATTEMPT_ID_RE.pattern}")
    return value


def _finite_positive(value: float) -> bool:
    """Shared duration validator: argparse's `type=float` passes `inf`/`nan`
    straight through unchanged, so every duration input — `run`'s
    --deadline-seconds and --grace-seconds, and `cancel`'s --grace-seconds
    — is checked through this one function, not reimplemented per
    subcommand."""
    return math.isfinite(value) and value > 0


def _receipt_path(receipt_dir: Path, attempt_id: str) -> Path:
    path = receipt_dir / f"{attempt_id}.json"
    resolved_dir = receipt_dir.resolve()
    resolved_path = path.resolve()
    if resolved_path.parent != resolved_dir:
        # A checked exception, never an assert: assertions are stripped
        # under `python -O`, and a containment check that can silently
        # vanish under an interpreter flag is not a safety check at all.
        # This is defense in depth behind _validated_attempt_id, which
        # every subcommand already runs first — this still fires if that
        # chokepoint is ever bypassed.
        raise ValueError(
            f"receipt path escaped receipt_dir: {resolved_path} not in {resolved_dir}")
    return path


def write_receipt(receipt_dir: Path, receipt: dict) -> None:
    """Atomic: a poller must never read a half-written receipt. The tmp path
    is unpredictable and created exclusively INSIDE receipt_dir via
    `tempfile.mkstemp` — O_CREAT|O_EXCL|0600 by construction — never a name
    derived from this writer's own pid. A `<receipt>.<pid>.tmp` name (the
    round-2 shape this replaces) is guessable the instant a caller reads
    supervisor_pid off any receipt this same process already wrote: a
    write-capable supervised child could plant a symlink at that predictable
    path ahead of time and have this process's own `os.replace` follow it
    onto an external file the child cannot otherwise touch. mkstemp's name
    cannot be predicted or pre-planted, and O_EXCL means it is never a
    symlink hop in the first place — the guessable pid-suffix name WAS the
    vulnerability, not a safeguard. The tmp is unlinked on any failure so a
    partial write never lingers next to the receipt."""
    path = _receipt_path(receipt_dir, receipt["attempt_id"])
    fd, tmp_name = tempfile.mkstemp(
        dir=receipt_dir, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(receipt, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_receipt(receipt_dir: Path, attempt_id: str) -> dict:
    return json.loads(_receipt_path(receipt_dir, attempt_id).read_text())


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but is not ours to signal — still alive


def _await_group_death(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return not _group_alive(pgid)


def terminate_group(proc: subprocess.Popen | None, pgid: int,
                    grace: float) -> bool:
    """TERM -> grace -> KILL -> confirm, against the whole group.

    Returns True only when the group is confirmed gone. The leader must be
    reaped (`proc.wait`) or a zombie holds the group open and a dead tree
    reads as alive forever.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        if proc is not None:
            try:
                proc.wait(timeout=grace)
                proc = None  # leader reaped
            except subprocess.TimeoutExpired:
                continue  # leader ignored the signal; escalate
        if _await_group_death(pgid, grace):
            return True
    return _await_group_death(pgid, grace)


def _validate_output(stdout_path: Path, output_schema: str) -> tuple[bool, str | None]:
    data = stdout_path.read_bytes()
    if not data.strip():
        return False, None
    digest = hashlib.sha256(data).hexdigest()
    if output_schema == "review" and not VERDICT_RE.search(
            data.decode(errors="replace")):
        return False, digest
    return True, digest


def _new_receipt(args, stdout_path: Path, stderr_path: Path) -> dict:
    return {
        "attempt_id": args.attempt_id,
        "seat": args.seat,
        "runtime": args.runtime,
        "model_id": args.model_id,
        "effort_native": args.effort_native,
        "permission_mode": args.permission_mode,
        "argv": args.argv,
        "prompt_sha256": None,
        "output_schema": args.output_schema,
        "process": {"pid": None, "process_group_id": None,
                    "supervisor_pid": os.getpid()},
        "timing": {"started_at": None, "deadline_at": None, "finished_at": None},
        "result": {
            "state": "STARTING", "exit_status": None,
            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
            "output_sha256": None, "schema_valid": None,
            "termination_confirmed": None,
        },
    }


def cmd_run(args) -> int:
    # Everything that can fail before spawn is validated before any receipt
    # exists — a preflight failure must never leave a permanent STARTING
    # receipt behind (status/cancel only know how to unwind RUNNING).
    try:
        args.attempt_id = _validated_attempt_id(args.attempt_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not _finite_positive(args.deadline_seconds):
        print(f"--deadline-seconds must be a finite number > 0, got "
              f"{args.deadline_seconds!r}", file=sys.stderr)
        return 2
    if not _finite_positive(args.grace_seconds):
        print(f"--grace-seconds must be a finite number > 0, got "
              f"{args.grace_seconds!r}", file=sys.stderr)
        return 2

    prompt_sha256 = None
    stdin_f = subprocess.DEVNULL
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        # Read (and hash) the prompt before anything is claimed or written:
        # a missing/unreadable prompt file must fail with nothing on disk,
        # not after a STARTING receipt already exists.
        try:
            prompt_bytes = prompt_path.read_bytes()
        except OSError as exc:
            print(f"--prompt-file {args.prompt_file!r} is not readable: "
                  f"{exc}", file=sys.stderr)
            return 2
        prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        stdin_f = open(prompt_path, "rb")

    receipt_dir = Path(args.receipt_dir)
    # 0o700: supervisor-owned, no other principal can read, write, or place
    # anything inside it. This is the out-of-band mitigation for the residual
    # O_TRUNC-on-hardlink risk noted at the stdout/stderr opens below —
    # O_NOFOLLOW refuses a symlink hop, but a hardlink is not a symlink and
    # still resolves to the external file's own inode, so O_TRUNC through it
    # still truncates that file. A 0700 directory this process exclusively
    # owns is what keeps anything else from placing a hardlink in here to
    # begin with.
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = _receipt_path(receipt_dir, args.attempt_id)
    claim_path = receipt_dir / f"{args.attempt_id}.claim"
    # Exclusive attempt creation is one atomic O_CREAT|O_EXCL claim on a
    # SEPARATE sentinel file (<attempt_id>.claim), never on the receipt path
    # itself. Claiming the receipt path directly would publish an empty
    # `.json` file at the final receipt pathname before the STARTING JSON
    # ever replaces it — a concurrent status/poller reading the receipt in
    # that window sees invalid JSON, and a supervisor crash in that same
    # window leaves a permanently unreadable "receipt". With the sentinel,
    # the receipt file itself only ever appears via write_receipt's atomic
    # tmp+os.replace of a COMPLETE JSON document — never half-written,
    # never empty, end to end. Two `run`s racing on the same attempt-id must
    # never both pass a check and then clobber each other's receipt/output
    # files; the loser of the claim gets FileExistsError as its refusal
    # signal, not a stale read.
    try:
        claim_f = open(claim_path, "x")
    except FileExistsError:
        print(f"attempt-id {args.attempt_id!r} is already claimed under "
              f"{receipt_dir} — another `run` is starting it, or a prior "
              f"claim was left behind by a crash (safe to remove once the "
              f"claiming supervisor is confirmed dead) — refusing to start "
              f"a second attempt with the same id", file=sys.stderr)
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
        return 2
    # The claim handle already owns the sentinel's filename; close it.
    claim_f.close()
    if receipt_path.exists():
        # A prior attempt under this id already ran to completion — its
        # own claim was already removed at its terminal write (below).
        # Attempt ids are one-shot: give up the claim just taken, which
        # belongs to no attempt, and refuse.
        print(f"attempt-id {args.attempt_id!r} already has a receipt under "
              f"{receipt_dir} — refusing to start a second attempt with the "
              f"same id", file=sys.stderr)
        claim_path.unlink(missing_ok=True)
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()
        return 2

    stdout_path = receipt_dir / f"{args.attempt_id}.stdout"
    stderr_path = receipt_dir / f"{args.attempt_id}.stderr"
    receipt = _new_receipt(args, stdout_path, stderr_path)
    receipt["prompt_sha256"] = prompt_sha256
    # deadline_at is computed here, before Popen — no post-spawn arithmetic
    # (e.g. a non-finite deadline) can crash once the child is already
    # running; the finiteness check above already rules that out, but
    # computing it before spawn keeps the ordering honest either way.
    #
    # deadline_monotonic is the SAME instant, stamped as a monotonic clock
    # reading instead of wall-clock: deadline_at is for humans reading the
    # receipt, deadline_monotonic is what every later wait actually
    # enforces against (monotonic is immune to wall-clock adjustment, and
    # is the only clock `time.monotonic()`-based waits below can compare
    # against consistently). Every wait from here on consumes the
    # REMAINING budget against this one anchor — never the full
    # deadline_seconds duration a second time — so a wait started partway
    # through an attempt cannot let it run past deadline_at.
    deadline_at = datetime.fromtimestamp(
        time.time() + args.deadline_seconds, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    deadline_monotonic = time.monotonic() + args.deadline_seconds
    write_receipt(receipt_dir, receipt)

    # stdout/stderr open without following a symlink: a regular open() that
    # follows one would let a symlink prepared at either path (by whatever
    # placed the prompt/receipt directory) truncate an arbitrary external
    # file this process can write to. O_NOFOLLOW refuses instead of
    # following.
    #
    # Residual risk O_NOFOLLOW does NOT close: a hardlink is not a symlink,
    # so O_NOFOLLOW does not refuse one — a hardlink at either path still
    # resolves straight to the external file's own inode, and O_TRUNC
    # through it still truncates that file. Nothing in this open() call can
    # tell a hardlink from an ordinary regular file. The mitigation is the
    # out-of-band 0700 mode receipt_dir was created with above: nothing but
    # this supervisor can place anything — symlink or hardlink — inside a
    # directory it exclusively owns.
    stdout_fd = None
    try:
        try:
            stdout_fd = os.open(
                stdout_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            stderr_fd = os.open(
                stderr_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            # Either open can fail (ELOOP on a planted symlink, a
            # permission failure, ENOSPC, ...). This arm must exist for the
            # SAME reason the Popen-failure arm below does: a STARTING
            # receipt already exists (written above) and nothing may leave
            # it as this attempt's last word. If the FIRST open (stdout)
            # succeeded and the SECOND (stderr) then raised, neither of the
            # two `with os.fdopen(...)` context managers below is ever
            # entered — so nothing else owns that first fd, and it would
            # leak here if not closed explicitly. The reason cannot be
            # written into the stderr file's own text (that file is
            # exactly what failed to open), so it goes to the supervisor's
            # own stderr instead.
            if stdout_fd is not None:
                os.close(stdout_fd)
            receipt["result"]["state"] = "START_FAILED"
            receipt["timing"]["finished_at"] = _utcnow()
            write_receipt(receipt_dir, receipt)
            # The receipt is terminal now — same rule as every other
            # terminal path: release the claim sentinel so it does not sit
            # forever as a claim with no attempt behind it.
            claim_path.unlink(missing_ok=True)
            print(f"start failed: cannot open output file: {exc}",
                  file=sys.stderr)
            return EXIT_BY_STATE["START_FAILED"]

        # stdout/stderr go straight to files: no pipe buffer to fill, no
        # drain thread to forget, no deadlock when the child floods.
        with os.fdopen(stdout_fd, "wb") as out_f, os.fdopen(stderr_fd, "wb") as err_f:
            try:
                proc = subprocess.Popen(
                    args.argv, stdin=stdin_f, stdout=out_f, stderr=err_f,
                    start_new_session=True)  # child leads its own group
            except OSError as exc:
                receipt["result"]["state"] = "START_FAILED"
                receipt["timing"]["finished_at"] = _utcnow()
                err_f.write(f"start failed: {exc}\n".encode())
                write_receipt(receipt_dir, receipt)
                # The receipt is terminal now — the claim sentinel has done
                # its job (kept a racing `run` from clobbering this attempt
                # while it was still ambiguous) and would otherwise sit
                # forever as a claim with no attempt behind it.
                claim_path.unlink(missing_ok=True)
                return EXIT_BY_STATE["START_FAILED"]

            pgid = proc.pid  # start_new_session: pgid == pid
            # supervisor_pid is this `run` process's own pid — distinct from
            # the child's pid/pgid above. It lets `status` tell "supervisor
            # dead, child alive" (orphaned) apart from "both dead" (stale):
            # without it, a RUNNING receipt only proves a child pgid existed,
            # never who is still watching the deadline. It is already set
            # (to this same os.getpid()) on the STARTING receipt above, so
            # `status` can triage a stuck STARTING receipt the same way.
            #
            # Everything from here to the terminal write is one try: a spawn
            # handle is not a result, and nothing after Popen succeeds may
            # leave the group unsupervised or the receipt stuck non-terminal
            # (below, `except Exception`).
            try:
                receipt["process"] = {"pid": proc.pid, "process_group_id": pgid,
                                      "supervisor_pid": os.getpid()}
                receipt["timing"]["started_at"] = _utcnow()
                receipt["timing"]["deadline_at"] = deadline_at
                receipt["result"]["state"] = "RUNNING"
                write_receipt(receipt_dir, receipt)

                try:
                    # Consume the REMAINING budget against the one monotonic
                    # anchor stamped before spawn (deadline_monotonic, Task 8)
                    # — not args.deadline_seconds again. Waiting the full
                    # duration a second time here would let the leader run
                    # past its own recorded deadline_at before TimeoutExpired
                    # ever fires; with the remaining-budget expression,
                    # TimeoutExpired now fires at the absolute deadline
                    # instant, not deadline_seconds after this wait started.
                    remaining = max(0.0, deadline_monotonic - time.monotonic())
                    exit_status = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    # Past the deadline nothing the attempt writes can matter:
                    # the state is decided by termination alone, and a late
                    # verdict on disk is deliberately never graded.
                    confirmed = terminate_group(proc, pgid, args.grace_seconds)
                    receipt["result"]["termination_confirmed"] = confirmed
                    receipt["result"]["state"] = (
                        "TIMED_OUT" if confirmed else "TERMINATION_UNCONFIRMED")
                else:
                    # The leader exited — but grandchildren may linger, and a
                    # lingering writer is the duplicate-writer hazard (F-02).
                    # Clean the group up and confirm before grading anything.
                    # Bound this confirmation wait by whatever deadline budget
                    # remains too, via min(args.grace_seconds, remaining) —
                    # grace is time to let a normal exit's stragglers finish
                    # dying, never free extra time before grading, so it must
                    # not let a normal exit outrun deadline_at. Grace keeps its
                    # full, un-shortened value only inside terminate_group's own
                    # TERM->grace->KILL escalation ladder below (that ladder
                    # already runs only once termination is being forced, past
                    # the point where "on time" still means anything).
                    remaining = max(0.0, deadline_monotonic - time.monotonic())
                    normal_exit_grace = min(args.grace_seconds, remaining)
                    confirmed = (_await_group_death(pgid, normal_exit_grace)
                                 or terminate_group(None, pgid, args.grace_seconds))
                    receipt["result"]["termination_confirmed"] = confirmed
                    receipt["result"]["exit_status"] = exit_status
                    if not confirmed:
                        receipt["result"]["state"] = "TERMINATION_UNCONFIRMED"
                    elif time.monotonic() > deadline_monotonic:
                        # Group confirmation itself can consume time —
                        # normal_exit_grace above, or the escalation ladder in
                        # terminate_group() when a grandchild lingers — and that
                        # wait can run past deadline_monotonic even though the
                        # leader exited 0 well before it. DD-9's invariant ("no
                        # output can produce SUCCEEDED once the deadline has
                        # expired") is about when GRADING happens, not just when
                        # the leader exited, so this re-check sits immediately
                        # before grading, not only in the TimeoutExpired branch
                        # above. exit_status is already recorded either way;
                        # schema_valid stays null — a post-deadline result is
                        # never graded, regardless of the leader's exit status.
                        receipt["result"]["state"] = "TIMED_OUT"
                    elif exit_status != 0:
                        receipt["result"]["state"] = "FAILED"
                    else:
                        ok, digest = _validate_output(stdout_path, args.output_schema)
                        receipt["result"]["output_sha256"] = digest
                        receipt["result"]["schema_valid"] = ok
                        receipt["result"]["state"] = (
                            "SUCCEEDED" if ok else "INVALID_OUTPUT")

                receipt["timing"]["finished_at"] = _utcnow()
                # DEFER-2: terminal persistence is best-effort. OSError here
                # must not skip claim release or borrow crash exit 9 — the
                # child is already reaped on this happy path. STARTING /
                # start-failed writes above stay strict.
                try:
                    write_receipt(receipt_dir, receipt)
                except OSError:
                    print("receipt write failed", file=sys.stderr)
                claim_path.unlink(missing_ok=True)  # receipt is terminal — release the claim, same as every other terminal path
                return EXIT_BY_STATE[receipt["result"]["state"]]
            except Exception:
                # A crash writing the RUNNING receipt, waiting on the child,
                # validating output, or writing the terminal receipt above
                # must still confirm the group is gone and leave a terminal
                # receipt behind — a supervisor crash must never abandon a
                # live process group behind a receipt stuck at RUNNING
                # (that would defeat DD-9/DD-10's duplicate-writer
                # protection exactly where it matters). Run the same
                # TERM->grace->KILL ladder, record whatever it confirms,
                # write the terminal receipt, then let the crash propagate —
                # `main()`'s guard still turns it into exit 9, but by then
                # the receipt is already terminal.
                confirmed = terminate_group(proc, pgid, args.grace_seconds)
                receipt["result"]["termination_confirmed"] = confirmed
                receipt["result"]["state"] = (
                    "CANCELLED" if confirmed else "TERMINATION_UNCONFIRMED")
                receipt["timing"]["finished_at"] = _utcnow()
                write_receipt(receipt_dir, receipt)
                claim_path.unlink(missing_ok=True)  # receipt is terminal now
                raise
    finally:
        if stdin_f is not subprocess.DEVNULL:
            stdin_f.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="supervise one attempt to a terminal state")
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--receipt-dir", required=True)
    run.add_argument("--deadline-seconds", type=float, required=True)
    run.add_argument("--grace-seconds", type=float, default=15.0)
    run.add_argument("--seat", required=True,
                     help="worker | reviewer-1 | reviewer-2 | judge")
    run.add_argument("--runtime", default=None)
    run.add_argument("--model-id", default=None)
    run.add_argument("--effort-native", default=None)
    run.add_argument("--permission-mode", default=None)
    run.add_argument("--prompt-file", default=None,
                     help="fed to the child's stdin; omit for DEVNULL")
    run.add_argument("--output-schema", choices=["none", "review"],
                     default="none")
    run.add_argument("argv", nargs="+",
                     help="command to execute, after `--`")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"run": cmd_run}
    try:
        return handlers[args.command](args)
    except Exception:  # noqa: BLE001 — a crash must not borrow an outcome code
        import traceback
        traceback.print_exc()
        return 9


if __name__ == "__main__":
    sys.exit(main())
