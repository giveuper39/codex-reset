"""Apply one explicitly selected, earliest-expiring Codex reset near expiry."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid


CONSUME = "account/rateLimitResetCredit/consume"
READ = "account/rateLimits/read"


class AppServer:
    def __init__(self, executable):
        self.executable = executable
        self.messages = queue.Queue()
        self.notifications = queue.Queue()
        self.sequence = 0

    def __enter__(self):
        self.proc = subprocess.Popen(
            [self.executable, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {
                "name": "codex_reset", "version": "unversioned"}})
            self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def read(self):
        try:
            for line in self.proc.stdout:
                message = json.loads(line)
                if "method" in message and "id" not in message:
                    self.notifications.put(message)
                else:
                    self.messages.put(message)
        except Exception as exc:
            self.messages.put(exc)
        finally:
            self.messages.put(EOFError("Codex app-server closed its output"))

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + 45
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for {method}") from exc
            if isinstance(message, Exception):
                raise message
            if "method" in message and "id" in message:
                raise RuntimeError("Codex requested interactive authentication; sign in again")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message["result"]

    def __exit__(self, *args):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


def supports_exact_credit(schema):
    for request in schema.get("oneOf", []):
        props = request.get("properties", {})
        if CONSUME not in props.get("method", {}).get("enum", []):
            continue
        params = props.get("params", {})
        if "$ref" in params:
            ref = params["$ref"]
            if not ref.startswith("#/"):
                return False
            params = schema
            for part in ref[2:].split("/"):
                params = params[part.replace("~1", "/").replace("~0", "~")]
        return "creditId" in params.get("properties", {})
    return False


def check_protocol(executable):
    with tempfile.TemporaryDirectory(prefix="codex-reset-schema-") as directory:
        subprocess.run(
            [executable, "app-server", "generate-json-schema", "--out", directory],
            check=True, timeout=45, stdout=subprocess.DEVNULL,
        )
        schema = json.loads((Path(directory) / "ClientRequest.json").read_text("utf-8"))
        if not supports_exact_credit(schema):
            raise RuntimeError("This Codex version cannot target creditId; refusing any reset")


def resolve_codex(command):
    executable = shutil.which(command)
    if executable and os.name == 'nt' and Path(executable).suffix.lower() in ('.cmd', '.bat', '.ps1'):
        package = Path(executable).parent / 'node_modules/@openai/codex/node_modules/@openai'
        candidates = list(package.glob('codex-win32-*/vendor/*/bin/codex.exe'))
        if len(candidates) != 1:
            raise RuntimeError('Pass the native codex.exe path; a shell wrapper cannot be used here')
        executable = str(candidates[0])
    return executable


def available_credits(snapshot):
    summary = snapshot.get("rateLimitResetCredits")
    if not isinstance(summary, dict) or not isinstance(summary.get("credits"), list):
        raise ValueError("Reset details unavailable; cannot safely choose a credit")
    rows = summary["credits"]
    count = summary.get("availableCount")
    if type(count) is not int or count < 0 or count != len(rows):
        raise ValueError("Reset list is incomplete; cannot establish the earliest expiry")
    ids = set()
    for row in rows:
        credit_id = row.get("id")
        if not isinstance(credit_id, str) or not credit_id or credit_id in ids:
            raise ValueError("Missing or duplicate reset ID")
        ids.add(credit_id)
        if row.get("status") != "available" or row.get("resetType") != "codexRateLimits":
            raise ValueError("Unknown reset type or status")
        # Missing dates are ambiguous. Explicit null means non-expiring, per protocol.
        if "expiresAt" not in row or (row["expiresAt"] is not None and
                                      type(row["expiresAt"]) is not int):
            raise ValueError("Unknown reset expiration")
    return rows


def earliest(snapshot, now):
    rows = [row for row in available_credits(snapshot)
            if row["expiresAt"] is not None and row["expiresAt"] > now]
    return min(rows, key=lambda row: (row["expiresAt"], row["id"]), default=None)


def eligible(snapshot, now):
    buckets = snapshot.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else snapshot.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        return False
    for field in ("primary", "secondary"):
        window = bucket.get(field) or {}
        used, resets = window.get("usedPercent"), window.get("resetsAt")
        if (window.get("windowDurationMins") in (300, 10080)
                and type(used) is int and 90 <= used <= 100
                and type(resets) is int and resets > now):
            return True
    return False


def save(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "watch.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def verify(snapshot, state):
    ids = {row["id"] for row in available_credits(snapshot)}
    if state["creditId"] in ids:
        raise RuntimeError("Server reported success but selected credit is still available")
    if not set(state["otherIds"]).issubset(ids):
        raise RuntimeError("Other reset IDs disappeared; manual review required")


def apply_reset(client, credit_id, path, before_minutes, now=None):
    state = json.loads(path.read_text("utf-8")) if path.exists() else None
    if state and state["creditId"] != credit_id:
        raise ValueError("State belongs to a different credit; refusing reset")
    if state and state["outcome"] in ("reset", "alreadyRedeemed"):
        if not state.get("verified"):
            verify(client.call(READ), state)
            state["verified"] = True
            save(path, state)
        print("Already completed and verified:", credit_id)
        return "alreadyCompleted"

    snapshot = client.call(READ)
    now = time.time() if now is None else now
    selected = earliest(snapshot, now)
    if state and not any(row["id"] == credit_id for row in available_credits(snapshot)):
        raise RuntimeError("Previous attempt is unresolved and credit is absent; manual review required")
    if not selected or selected["id"] != credit_id:
        print("Waiting: authorized credit is not the earliest live expiring reset")
        return
    if selected["expiresAt"] - now > before_minutes * 60:
        print("Waiting: selected reset is not near expiry")
        return
    if not eligible(snapshot, now):
        print("Waiting: no current five-hour/weekly window has <=10% remaining")
        return
    if not state:
        state = {"creditId": credit_id, "idempotencyKey": str(uuid.uuid4()),
                 "expiresAt": selected["expiresAt"], "outcome": "pending",
                 "otherIds": [row["id"] for row in available_credits(snapshot)
                              if row["id"] != credit_id]}
        save(path, state)  # Persist BEFORE the request, including on uncertain responses.
    # creditId is mandatory here. Never let the service choose an arbitrary credit.
    result = client.call(CONSUME, {"creditId": state["creditId"],
                                   "idempotencyKey": state["idempotencyKey"]})
    outcome = result.get("outcome")
    if outcome not in ("reset", "alreadyRedeemed", "nothingToReset", "noCredit"):
        raise RuntimeError(f"Unknown reset outcome: {outcome!r}")
    if outcome in ("reset", "alreadyRedeemed"):
        state["outcome"] = outcome
        save(path, state)  # Success remains terminal even if the refresh fails.
        verify(client.call(READ), state)
        state["verified"] = True
        save(path, state)
        print("Applied and verified exact credit:", credit_id)
        return outcome
    else:
        print("No reset applied:", outcome)
        # A definitive refusal ends this attempt. A later eligible attempt gets a new key.
        state["idempotencyKey"] = str(uuid.uuid4())
        save(path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Codex executable path")
    parser.add_argument("--apply", action="store_true", help="Permit one named reset to be consumed")
    parser.add_argument("--credit-id", help="Explicitly authorized banked reset ID")
    parser.add_argument("--before-minutes", type=int, default=15)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/codex-reset-bot/redemptions")
    args = parser.parse_args()
    if args.apply and not args.credit_id:
        parser.error("--apply requires --credit-id; automatic credit selection is forbidden")
    if not 1 <= args.before_minutes <= 1440:
        parser.error("--before-minutes must be between 1 and 1440")
    executable = resolve_codex(args.codex)
    if not executable:
        parser.error("Codex executable not found")
    with locked(args.state_dir):
        if args.apply:
            check_protocol(executable)
        with AppServer(executable) as client:
            if args.apply:
                digest = hashlib.sha256(args.credit_id.encode()).hexdigest()
                apply_reset(client, args.credit_id, args.state_dir / (digest + ".json"), args.before_minutes)
            else:
                snapshot = client.call(READ)
                selected = earliest(snapshot, time.time())
                for row in sorted(available_credits(snapshot), key=lambda c: (
                        c["expiresAt"] is None, c["expiresAt"] or 0, c["id"])):
                    expiry = (datetime.fromtimestamp(row["expiresAt"], timezone.utc).isoformat()
                              if row["expiresAt"] is not None else "never")
                    print(json.dumps({"creditId": row["id"], "expiresAtUTC": expiry,
                                      "earliest": bool(selected and selected["id"] == row["id"])}))
                print("Eligible now:", eligible(snapshot, time.time()))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
