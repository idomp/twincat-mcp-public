"""Live qualification harness: drives mcp-server/server.py through a real stdio MCP client.

Every call goes to calls.jsonl before it is sent, and every receipt after it arrives. Each
server gets its own TWINCAT_MCP_STATE_DIR under the run folder, never the real one. Restart
tests reopen the folder of a server that has already stopped; no two live servers share one.
"""
import asyncio
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent
V3 = HERE.parent
PY = V3 / ".venv" / "Scripts" / "python.exe"
SERVER = V3 / "mcp-server" / "server.py"
WORKER = V3 / "TcAutomation" / "bin" / "Release-v2" / "TcAutomation.exe"
REAL_STATE = (Path(os.environ["LOCALAPPDATA"]) / "twincat-mcp").resolve()
ROUTES = Path(r"C:\TwinCAT\3.1\Target\StaticRoutes.xml")
TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}


def _inside(path, root):
    return path == root or root in path.parents


def _qual_dir():
    raw = os.environ.get("QUAL_DIR", "")
    path = Path(raw)
    if not raw or not path.is_absolute() or not path.is_dir():
        raise SystemExit("QUAL_DIR must name an existing absolute folder outside the repository.")
    path = path.resolve()
    for root in (V3, REAL_STATE):
        if _inside(path, root) or _inside(root, path):
            raise SystemExit(f"QUAL_DIR must not overlap {root}.")
    return path


QUAL = _qual_dir()  # fixtures, runs and site.json; checked before anything is written


def _no_blanks(value, where="site.json"):
    if value is None or value == "" or value == {} or value == []:
        raise SystemExit(f"{where} is empty. Every site value must be set.")
    if isinstance(value, dict):
        for k, v in value.items():
            _no_blanks(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _no_blanks(v, f"{where}[{i}]")


def site():
    """Target, route, project and symbol names of the live target. They never go into the repository."""
    data = json.loads((QUAL / "site.json").read_text(encoding="utf-8"))
    _no_blanks(data)
    return data


def route_netid(name):
    """Net ID of the one static route with this name, or None when there is not exactly one."""
    routes = ET.parse(ROUTES).getroot().findall("./RemoteConnections/Route")
    match = [r.findtext("NetId", "").strip() for r in routes if r.findtext("Name", "").strip() == name]
    return match[0] if len(match) == 1 else None


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    out = subprocess.run(["git", "-C", str(V3), *args], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


class Run:
    def __init__(self, name):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.id = f"{stamp}-{name}"
        runs = QUAL / "runs"
        runs.mkdir(exist_ok=True)
        if not _inside(runs.resolve(), QUAL):  # a junction can point elsewhere
            raise SystemExit(f"{runs} must resolve inside QUAL_DIR.")
        self.dir = runs.resolve() / self.id
        self.dir.mkdir()
        self.log = open(self.dir / "calls.jsonl", "a", encoding="utf-8")
        self.results = []
        site_file = QUAL / "site.json"
        self.identity = dict(
            runId=self.id,
            sourceCommit=git("rev-parse", "HEAD"),
            sourceDirty=bool(git("status", "--porcelain", "--untracked-files=no")),
            harnessSha256={p.name: sha256(p)[:16] for p in sorted(HERE.glob("*.py"))},
            siteSha256=sha256(site_file)[:16] if site_file.exists() else None,
            workerSha256=sha256(WORKER) if WORKER.exists() else None,
            python=str(PY),
        )
        self.write(dict(phase="identity", **self.identity))

    def write(self, record):
        record = dict(utc=datetime.now(timezone.utc).isoformat(), monotonicNs=time.monotonic_ns(), **record)
        self.log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.log.flush()

    def record(self, test, tier, status, detail="", **evidence):
        assert status in {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "EXPECTED_UNAVAILABLE"}
        self.results.append(dict(test=test, tier=tier, status=status, detail=detail, evidence=evidence))
        self.write(dict(phase="verdict", testId=test, tier=tier, status=status, detail=detail, evidence=evidence))
        print(f"[{status}] {test} (tier {tier}) {detail}", flush=True)

    def report(self, title):
        (self.dir / "results.json").write_text(
            json.dumps(dict(identity=self.identity, results=self.results), indent=2, default=str), encoding="utf-8"
        )
        counts = {}
        for r in self.results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        lines = [f"# {title}", "", f"Run `{self.id}`.", ""]
        lines += [f"- {k}: `{v}`" for k, v in self.identity.items()]
        lines += ["", "| Result | Count |", "| --- | --- |"] + [f"| {k} | {v} |" for k, v in sorted(counts.items())]
        lines += ["", "| Test | Tier | Result | Detail |", "| --- | --- | --- | --- |"]
        lines += [f"| {r['test']} | {r['tier']} | {r['status']} | {' '.join(r['detail'].replace('|', '/').split())} |"
                  for r in self.results]
        (self.dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return counts

    def finish(self, title):
        """Write the report. Exit 1 on any FAIL or BLOCKED result, or when nothing was recorded."""
        counts = self.report(title)
        print(json.dumps(counts), self.dir)
        sys.exit(1 if not counts or counts.get("FAIL") or counts.get("BLOCKED") else 0)


def isolated_state(run, name):
    path = (run.dir / "state" / name).resolve()
    if _inside(path, REAL_STATE) or not _inside(path, run.dir.resolve()):
        raise RuntimeError("State folder must sit inside the run folder.")
    path.mkdir(parents=True)
    return path


class Client:
    def __init__(self, run, name, session, state_dir):
        self.run, self.name, self.session, self.state_dir = run, name, session, state_dir
        self.test = None

    async def tool(self, tool, args, timeout=200):
        rid = uuid.uuid4().hex[:12]
        self.run.write(dict(phase="call", client=self.name, testId=self.test, requestId=rid, tool=tool, payload=args))
        try:
            res = await asyncio.wait_for(self.session.call_tool(tool, args), timeout)
        except Exception as exc:  # protocol-level error, recorded as such
            self.run.write(dict(phase="receipt", client=self.name, testId=self.test, requestId=rid, protocolError=repr(exc)))
            return dict(protocolError=repr(exc)), True
        raw = getattr(res.content[0], "text", None) if res.content else None
        self.run.write(dict(phase="receipt", client=self.name, testId=self.test, requestId=rid, isError=res.is_error,
                            structured=res.structured_content, text=raw))
        try:
            text = json.loads(raw) if raw is not None else None
        except ValueError as exc:
            self.run.write(dict(phase="decode_error", client=self.name, requestId=rid, error=repr(exc)))
            text = None
        payload = res.structured_content if res.structured_content is not None else text
        if payload is None:
            return dict(protocolError="receipt has no structured content and no JSON text"), True
        if text is not None and text != payload:
            self.run.write(dict(phase="mismatch", client=self.name, requestId=rid))
        return payload, bool(res.is_error)

    async def ctl(self, op, args=None, timeout=200):
        payload, _ = await self.tool("twincat_execute", {"operation": op, "arguments": args or {}}, timeout)
        return payload

    async def submit(self, op, args, key=None, wait=0):
        key = key or str(uuid.uuid4())
        payload, _ = await self.tool(
            "twincat_execute", {"operation": op, "arguments": args, "requestKey": key, "waitSeconds": wait}
        )
        return payload, key

    async def run_op(self, op, args, key=None, timeout=600):
        """Submit and wait. A receipt that is not terminal carries unresolved=True with its job handle and key."""
        deadline = time.monotonic() + timeout
        receipt, key = await self.submit(op, args, key)
        if receipt.get("protocolError"):  # the job can exist although its receipt was lost
            return dict(unresolved=True, requestKey=key, submitError=receipt["protocolError"])
        handle = receipt.get("jobHandle")
        while handle and receipt.get("state") not in TERMINAL:
            left = deadline - time.monotonic()
            if left <= 0:
                return dict(receipt, unresolved=True, requestKey=key)
            wait = int(max(1, min(30, left)))
            nxt = await self.ctl("job.wait", {"jobHandle": handle, "waitSeconds": wait}, timeout=wait + 30)
            if "state" not in nxt:  # the wait failed; the job itself is still unknown
                return dict(receipt, unresolved=True, requestKey=key, waitError=nxt)
            receipt = nxt
        if receipt.get("resultAvailable") and "result" not in receipt:
            receipt["result"] = await self.full_result(handle)
        return receipt

    async def full_result(self, handle, limit=8000, max_pages=1000):
        text, offset = "", 0
        for _ in range(max_pages):
            page = await self.ctl("job.result", {"jobHandle": handle, "offset": offset, "limit": limit})
            if "text" not in page:
                raise RuntimeError(f"job.result for {handle} at offset {offset} failed: {json.dumps(page)[:300]}")
            text += page["text"]
            nxt = page.get("nextOffset")
            if nxt is None:
                return json.loads(text)
            if nxt <= offset:
                raise RuntimeError(f"job.result for {handle} did not advance past offset {offset}")
            offset = nxt
        raise RuntimeError(f"job.result for {handle} exceeded {max_pages} pages")

    def session_records(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.state_dir.glob("session-*.json")]


@asynccontextmanager
async def server(run, name, env_extra=None, state_dir=None):
    """state_dir reopens the folder of a stopped server of this run (restart tests only)."""
    extra = dict(env_extra or {})
    if "TWINCAT_MCP_STATE_DIR" in extra:
        raise RuntimeError("env_extra must not set TWINCAT_MCP_STATE_DIR.")
    if state_dir is not None:
        state_dir = Path(state_dir).resolve()
        if not _inside(state_dir, (run.dir / "state").resolve()) or not state_dir.is_dir():
            raise RuntimeError("state_dir must be an existing state folder of this run.")
    state_dir = state_dir or isolated_state(run, name)
    env = dict(os.environ)
    env.update(TWINCAT_AUTOMATION_EXE=str(WORKER), PYTHONUTF8="1")
    env.pop("TWINCAT_DISABLE_HOST", None)
    env.update(extra)
    env["TWINCAT_MCP_STATE_DIR"] = str(state_dir)
    errlog = open(run.dir / f"stderr-{name}-{int(time.time())}.log", "w", encoding="utf-8")
    params = StdioServerParameters(command=str(PY), args=[str(SERVER)], env=env, cwd=str(SERVER.parent))
    run.write(dict(phase="server_start", client=name, stateDir=env["TWINCAT_MCP_STATE_DIR"], envExtra=extra))
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield Client(run, name, session, state_dir)
    finally:
        run.write(dict(phase="server_stop", client=name))
        errlog.close()


# One process handle pins the process from the identity check to the kill, so a reused PID
# cannot be hit. Creation times compare to the microsecond.
_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_K32.OpenProcess.restype = wintypes.HANDLE
_K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_K32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_K32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_K32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
_K32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_K32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_K32.CloseHandle.argtypes = (wintypes.HANDLE,)
_QUERY, _TERMINATE, _SYNCHRONIZE, _STILL_ACTIVE = 0x1000, 0x0001, 0x00100000, 259


def _open(pid, access):
    return _K32.OpenProcess(access, False, int(pid)) if pid else None


def _created(handle):
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    if not _K32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return None
    ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime  # 100 ns since 1601
    return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)


def _running(handle):
    code = wintypes.DWORD()
    return bool(_K32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE


def _image(handle):
    size = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    return buf.value if _K32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None


def _parse_utc(text):
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", str(text or ""))
    if not m:
        return None
    when = datetime.fromisoformat(m.group(1) + "." + (m.group(2) or "0").ljust(6, "0")[:6] + (m.group(3) or "Z").replace("Z", "+00:00"))
    return when.astimezone(timezone.utc)


def process_start_utc(pid):
    """Creation time of a running process as ISO text, or None."""
    h = _open(pid, _QUERY)
    if not h:
        return None
    try:
        when = _created(h) if _running(h) else None
        return when.isoformat() if when else None
    finally:
        _K32.CloseHandle(h)


def same_process(pid, start_utc):
    """True when pid runs and its creation time equals start_utc to the microsecond."""
    want = _parse_utc(start_utc)
    h = _open(pid, _QUERY) if want else None
    if not h:
        return False
    try:
        return _running(h) and _created(h) == want
    finally:
        _K32.CloseHandle(h)


def ancestors(pid, depth=3):
    """Parent PIDs of pid, nearest first, as far as depth. A venv python.exe starts the real one."""
    out = []
    for _ in range(depth):
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').ParentProcessId"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode or not r.stdout.strip().isdigit():
            break
        pid = int(r.stdout.strip())
        out.append(pid)
    return out


def kill_pinned(pid, start_utc, image_names):
    """Kill pid only if it runs, its creation time equals start_utc to the microsecond, and its
    executable name is in image_names. Returns (killed, detail)."""
    want = _parse_utc(start_utc)
    if not want:
        return False, f"no recorded start time ({start_utc!r})"
    h = _open(pid, _QUERY | _TERMINATE | _SYNCHRONIZE)
    if not h:
        return False, f"cannot open PID {pid} (error {ctypes.get_last_error()})"
    try:
        actual, image = _created(h), _image(h)
        if not _running(h):
            return False, f"PID {pid} has exited"
        if actual != want:
            return False, f"PID {pid} started {actual}, recorded {want}"
        if not image or Path(image).name.lower() not in {n.lower() for n in image_names}:
            return False, f"PID {pid} runs {image}, not {sorted(image_names)}"
        if not _K32.TerminateProcess(h, 1):
            return False, f"TerminateProcess failed (error {ctypes.get_last_error()})"
        if _K32.WaitForSingleObject(h, 10000) != 0:
            return False, f"PID {pid} still runs 10 s after TerminateProcess"
        return True, f"{image} started {actual}"
    finally:
        _K32.CloseHandle(h)


def check(cond, message):
    if not cond:
        raise AssertionError(message)


class Blocked(Exception):
    """A precondition is not met; the test did not run its operation."""


async def guarded(run, test, tier, coro):
    """Record PASS only when the coroutine returns. Returns True on PASS."""
    try:
        detail = await coro
        run.record(test, tier, "PASS", detail or "")
        return True
    except Blocked as exc:
        run.record(test, tier, "BLOCKED", str(exc))
    except AssertionError as exc:
        run.record(test, tier, "FAIL", str(exc))
    except Exception as exc:  # harness or environment error
        run.record(test, tier, "FAIL", f"error: {exc!r}")
    return False


def execute(name, title, tier, main):
    """Run main(run), always write the report, and exit 1 on any FAIL or BLOCKED result."""
    try:
        run = Run(name)
    except Exception as exc:
        sys.exit(f"Cannot start the run: {exc!r}")
    try:
        asyncio.run(main(run))
    except BaseException as exc:  # an abort is a result too
        run.record("RUNNER", tier, "FAIL", f"aborted: {exc!r}")
    run.finish(title)


if __name__ == "__main__":
    sys.exit("Import this module from a tier runner.")
