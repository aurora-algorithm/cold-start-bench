#!/usr/bin/env python3
"""The cold-start measurement session, natively on Windows (i7-13700F), no Docker, no WSL.

One orders API (GET /orders/{id}, byte-identical JSON on every side, checked every cell), six ways to run it:
  spring         Spring Boot 4.1 on the JVM (Temurin 25, java -jar, every JVM setting default)
  spring-aot     the same jar, extracted, with a JDK 25 AOT cache from a training run (Project Leyden)
  spring-native  the same app compiled by GraalVM native-image (Spring AOT, Serial GC, the defaults)
  cs-jit         ASP.NET Core minimal API, the usual JIT build
  cs-aot         the same code published with Native AOT
  go             Go net/http

Per round, every side (order rotates each round) runs two cells:
  start  hyperfine runs the probe RUNS times (2 warm-ups): the probe starts the server and stops the clock at its
         first 200 on /orders/1, then kills it; hyperfine's --prepare waits until that process has gone.
  load   a fresh server: idle memory after IDLE_S, then oha at full speed for RAMP_S right away (every request
         kept, per 0.1 s: the JIT warm-up curve), then STEADY_S more at full speed (the warmed-up rate), then
         CAP_S at a fixed CAP_RATE for every side (memory under the same load). Memory sampled every 0.5 s.
The server runs on logical CPUs 0,2,4,6 (four performance cores, their SMT siblings idle), the probe and oha on
8-15; the E-cores (16-23) are left alone. Rounds 1..N record the live screens (hyperfine and the ramp's oha) as
asciinema casts through a Windows pseudo-console; a last "plain" round runs without recording, to check that the
screens didn't move the numbers.

usage (from C:/bench/cold-start, after tools/build.sh):  python tools/session.py RESULTS_DIR [ROUNDS=5]
env:  BENCH_SHORT=1 short phases (pilot only), BENCH_NO_GATE=1 skip the quiet gate (pilot only),
      BENCH_SIDES=spring,go (pilot only)
"""
import hashlib
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
TOOLS = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PY = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable
PIN, CAST = str(TOOLS / "pin.py"), str(TOOLS / "cast.py")
LINKS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links"
OHA, HYPERFINE = str(LINKS / "oha.exe"), str(LINKS / "hyperfine.exe")
JAVA_HOME = Path(os.environ.get("BENCH_JAVA_HOME") or next(Path("C:/Program Files/Eclipse Adoptium").glob("jdk-25*")))
JAVA = (JAVA_HOME / "bin/java.exe").as_posix()
PROBE = (BUILD / "probe.exe").as_posix()

SERVER_CPUS, CLIENT_CPUS = "0,2,4,6", "8-15"
GATE_CPUS = list(range(16))
PORT = 18080
BASE = f"http://127.0.0.1:{PORT}"
ORDER_URL = BASE + "/orders/[1-9][0-9]{0,4}"
CHECK_PATHS = ["/orders/1", "/orders/777", "/orders/100000"]
CONNECTIONS = 64
CAP_RATE = 5000
SHORT = os.environ.get("BENCH_SHORT") == "1"
RUNS, IDLE_S, RAMP_S, STEADY_S, CAP_S = (5, 3, 6, 6, 6) if SHORT else (20, 5, 20, 20, 20)
BIN_S = 0.1
START_COLS, START_ROWS = 52, 4  # hyperfine's progress line and result fit 52 columns (099's casts)
OHA_COLS, OHA_ROWS = 64, 14  # oha's live screen at a size that stays readable on a phone (109/110/098)

# Tomcat closes a connection after 100 requests by default; at 100k req/s that leaves ~20,000 loopback sockets in
# TIME_WAIT and Windows runs out of local ports for oha (os error 10048). Go and ASP.NET Core keep
# connections open, so the Spring sides do too.
KEEP_ALIVE = "--server.tomcat.max-keep-alive-requests=-1"
SIDES = {
    # name: (language, label on screen, argv)
    "spring": ("java", "Spring Boot, JVM", [JAVA, "-jar", (BUILD / "orders.jar").as_posix(), KEEP_ALIVE]),
    "spring-aot": ("java", "Spring Boot, JVM + AOT cache",
                   [JAVA, f"-XX:AOTCache={(BUILD / 'orders.aot').as_posix()}", "-jar",
                    (BUILD / "extracted/orders.jar").as_posix(), KEEP_ALIVE]),
    "spring-native": ("java", "Spring Boot, native image", [(BUILD / "orders-native.exe").as_posix(), KEEP_ALIVE]),
    "cs-jit": ("csharp", "ASP.NET Core, JIT", [(BUILD / "cs-jit/orders.exe").as_posix()]),
    "cs-aot": ("csharp", "ASP.NET Core, Native AOT", [(BUILD / "cs-aot/orders.exe").as_posix()]),
    "go": ("go", "Go net/http", [(BUILD / "go-orders.exe").as_posix()]),
}


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def quiet_gate(max_wait: int = 1800) -> dict:
    """Waits until the 16 logical CPUs the session uses average under 5% (none over 15%) over 5 s and the whole
    machine is under 10%; returns that reading. Gives up after max_wait: a busy machine voids the numbers, so the
    session never measures through it."""
    if os.environ.get("BENCH_NO_GATE") == "1":
        return {"gate": "skipped", "total": psutil.cpu_percent(interval=1)}
    waited = 0
    while True:
        per = psutil.cpu_percent(interval=5, percpu=True)
        ours = [per[c] for c in GATE_CPUS]
        reading = {"ours_mean": round(sum(ours) / len(ours), 1), "busiest_used_cpu": max(ours),
                   "total": round(sum(per) / len(per), 1)}
        # One Windows background thread hops between cores at 6-12% while the machine sits near 1%.
        if reading["ours_mean"] < 5 and reading["busiest_used_cpu"] < 15 and reading["total"] < 10:
            return reading
        if waited >= max_wait:
            raise RuntimeError(f"machine stayed busy for {max_wait} s: {reading}")
        if waited % 300 == 0:
            log(f"machine busy {reading}, waiting")
        waited += 5


def get(path: str, timeout: float = 5) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"{path}: HTTP {r.status}")
        return r.read()


def port_free() -> bool:
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.05).close()
        return False
    except OSError:
        return True


def posix_join(argv: list[str]) -> str:
    """hyperfine -N splits a command like a POSIX shell: quote what holds spaces, never use backslashes."""
    return " ".join(f"'{a}'" if any(c in a for c in " ;&|") else a for a in argv)


def start_cell(side: str, rdir: Path, record: bool) -> dict:
    _, label, argv = SIDES[side]
    name = f"start-{side}"
    export, probe_log, pid = rdir / f"{name}.json", rdir / f"{name}.probe.jsonl", rdir / f"{name}.pid"
    probe_cmd = posix_join([PROBE, "-label", side, "-log", probe_log.as_posix(), "-pidfile", pid.as_posix(),
                            "-port", str(PORT), "--", *argv])
    hf = [PY, PIN, CLIENT_CPUS, HYPERFINE, "-N", "--warmup", "2", "--runs", str(RUNS),
          "--prepare", posix_join([PROBE, "-wait", pid.as_posix()]), "-n", label, "--export-json", str(export),
          probe_cmd]
    env = {**os.environ, "SERVER_CPUS": SERVER_CPUS, "PROBE_CPUS": CLIENT_CPUS, "PORT": str(PORT)}
    if not port_free():
        raise RuntimeError(f"port {PORT} already answers before {name}")
    busy = quiet_gate()
    t0 = time.time()
    if record:
        rc = subprocess.call([PY, CAST, str(rdir / f"{name}.cast"), str(START_COLS), str(START_ROWS), "--", *hf],
                             env=env, creationflags=NO_WINDOW)
    else:
        rc = subprocess.call(hf, env=env, stdout=subprocess.DEVNULL, creationflags=NO_WINDOW)
    subprocess.call([PROBE, "-wait", str(pid)], creationflags=NO_WINDOW)
    if rc != 0 or not export.exists():
        raise RuntimeError(f"{name}: hyperfine failed ({rc})")
    report = json.loads(export.read_text())["results"][0]
    wall = [m["time_wall_clock"]["value"] for m in report["measurements"]]
    if any(m["exit_code"] != 0 for m in report["measurements"]) or report["measurements"][0]["time_wall_clock"]["unit"] != "second":
        raise RuntimeError(f"{name}: unexpected hyperfine export")
    probes = [json.loads(l) for l in probe_log.read_text().splitlines() if l.strip()]
    measured = probes[-len(wall):]  # the first lines are hyperfine's warm-up runs
    shas = {p["body_sha256"] for p in probes}
    if len(shas) != 1 or any(p["status"] != 200 for p in probes):
        raise RuntimeError(f"{name}: first answers differ {shas}")
    return {"cell": name, "kind": "start", "side": side, "lang": SIDES[side][0], "label": label, "started": t0,
            "busy_before": busy, "wall_s": wall, "first_ms": [p["first_ms"] for p in measured],
            "listen_ms": [p["listen_ms"] for p in measured], "first_sha256": shas.pop(), "runs": len(wall)}


class Server:
    """One server process pinned to SERVER_CPUS from its first instruction (pin.py sets the mask, the server
    inherits it), with its start-up time and a memory sampler."""

    def __init__(self, side: str, log_file: Path):
        _, _, argv = SIDES[side]
        if not port_free():
            raise RuntimeError(f"port {PORT} already answers: a server from an earlier cell is still running")
        self.out = open(log_file, "wb")  # noqa: SIM115 -- the server writes to it until stop() closes it
        self.pin = subprocess.Popen([PY, PIN, SERVER_CPUS, *argv], stdout=self.out, stderr=subprocess.STDOUT,
                                    env={**os.environ, "PORT": str(PORT)}, creationflags=NO_WINDOW)
        try:
            self._wait_ready()
            ready_wall = time.time()
            children = [c for c in psutil.Process(self.pin.pid).children() if c.name().lower() != "conhost.exe"]
            if len(children) != 1:
                raise RuntimeError(f"{side}: expected one server process, found {children}")
        except BaseException:
            self._kill_tree()
            raise
        self.proc = children[0]
        self.ready_s = ready_wall - self.proc.create_time()
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None

    def _wait_ready(self) -> None:
        deadline = time.perf_counter() + 60
        while time.perf_counter() < deadline:
            if self.pin.poll() is not None:
                raise RuntimeError("server exited during start-up")
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.01).close()
            except OSError:
                continue
            try:
                get("/orders/1", timeout=10)
                return
            except OSError:
                time.sleep(0.002)
        raise RuntimeError("server did not answer within 60 s")

    def memory(self) -> dict:
        m = self.proc.memory_full_info()
        return {"working_set": m.rss, "private": m.private, "threads": self.proc.num_threads()}

    def start_sampling(self, phase: str) -> None:
        def run() -> None:
            t0 = time.perf_counter()
            while not self._stop.is_set():
                c = self.proc.cpu_times()
                self.samples.append({"phase": phase, "t": round(time.perf_counter() - t0, 3), **self.memory(),
                                     "cpu_s": c.user + c.system})
                self._stop.wait(0.5)
        self._stop.clear()
        self._sampler = threading.Thread(target=run, daemon=True)
        self._sampler.start()

    def stop_sampling(self) -> None:
        self._stop.set()
        if self._sampler:
            self._sampler.join()

    def _kill_tree(self) -> None:
        try:
            procs = psutil.Process(self.pin.pid).children(recursive=True) + [psutil.Process(self.pin.pid)]
        except psutil.NoSuchProcess:
            procs = []
        for p in procs:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=30)
        self.pin.wait(timeout=30)
        self.out.close()

    def kill(self) -> None:
        self._kill_tree()
        for _ in range(100):
            if port_free():
                return
            time.sleep(0.1)
        raise RuntimeError("old server still answers after kill")


def oha_argv(seconds: int, db: Path | None, rate: int | None, tui: bool, out_json: bool) -> list[str]:
    argv = [PY, PIN, CLIENT_CPUS, OHA, "-z", f"{seconds}s", "-c", str(CONNECTIONS), "--rand-regex-url", ORDER_URL]
    if rate:
        argv += ["-q", str(rate), "--latency-correction"]
    if db:
        argv += ["--db-url", str(db)]
    if not tui:
        argv += ["--no-tui"]
    if out_json:
        argv += ["--output-format", "json"]
    return argv


def run_oha(argv: list[str], cast: Path | None, out_json: Path | None) -> dict | None:
    if cast:
        rc = subprocess.call([PY, CAST, str(cast), str(OHA_COLS), str(OHA_ROWS), "--", *argv], creationflags=NO_WINDOW)
    else:
        with open(out_json, "wb") if out_json else open(os.devnull, "wb") as f:
            rc = subprocess.call(argv, stdout=f, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if rc != 0:
        raise RuntimeError(f"oha failed ({rc})")
    return json.loads(out_json.read_text(encoding="utf-8")) if out_json else None


def summary(report: dict, seconds: int) -> dict:
    codes = report.get("statusCodeDistribution", {})
    if set(codes) != {"200"}:
        raise RuntimeError(f"non-200 responses {codes} {report.get('errorDistribution')}")
    pct = report["latencyPercentiles"]
    return {"rps": report["summary"]["requestsPerSec"], "ok": codes["200"], "seconds": seconds,
            "p50_ms": pct["p50"] * 1000, "p99_ms": pct["p99"] * 1000, "p999_ms": pct["p99.9"] * 1000}


def reduce_db(db: Path, seconds: int) -> dict:
    """Per-0.1 s finished requests, status counts and latency percentiles from oha's per-request table."""
    con = sqlite3.connect(db)
    try:
        statuses = dict(con.execute("SELECT status, COUNT(*) FROM oha GROUP BY status").fetchall())
        bins = [0] * round(seconds / BIN_S)
        for (b, n) in con.execute(f"SELECT CAST(end / {BIN_S} AS INTEGER), COUNT(*) FROM oha WHERE status = 200 "
                                  f"GROUP BY 1").fetchall():
            if 0 <= b < len(bins):
                bins[b] = n
        durations = [d for (d,) in con.execute("SELECT duration FROM oha WHERE status = 200 ORDER BY duration")]
    finally:
        con.close()
    if set(statuses) != {200}:
        raise RuntimeError(f"ramp statuses {statuses}")
    ok = statuses[200]
    pct = {f"p{q}_ms": durations[min(len(durations) - 1, int(len(durations) * q / 100))] * 1000 for q in (50, 99, 99.9)}
    return {"ok": ok, "rps": ok / seconds, "seconds": seconds, **pct, "per_bin": bins, "bin_s": BIN_S}


def median(xs: list[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2] if s else 0


def load_cell(side: str, rdir: Path, record: bool) -> dict:
    name = f"load-{side}"
    busy = quiet_gate()
    server = Server(side, rdir / f"{name}.server.log")
    try:
        time.sleep(IDLE_S)
        server.start_sampling("idle")
        time.sleep(3)
        server.stop_sampling()
        db = rdir / f"{name}.db"
        server.start_sampling("ramp")
        t0 = time.time()
        run_oha(oha_argv(RAMP_S, db, None, record, False), rdir / f"{name}.cast" if record else None, None)
        server.stop_sampling()
        ramp = reduce_db(db, RAMP_S)
        db.unlink()
        server.start_sampling("steady")
        steady = summary(run_oha(oha_argv(STEADY_S, None, None, False, True), None, rdir / f"{name}.steady.json"), STEADY_S)
        server.stop_sampling()
        server.start_sampling("capped")
        capped = summary(run_oha(oha_argv(CAP_S, None, CAP_RATE, False, True), None, rdir / f"{name}.capped.json"), CAP_S)
        server.stop_sampling()
        bodies = {p: hashlib.sha256(get(p)).hexdigest() for p in CHECK_PATHS}
    finally:
        server.kill()
    mem = {}
    for phase in ("idle", "ramp", "steady", "capped"):
        rows = [s for s in server.samples if s["phase"] == phase]
        mem[phase] = {"working_set_median": median([s["working_set"] for s in rows]),
                      "working_set_max": max(s["working_set"] for s in rows),
                      "private_median": median([s["private"] for s in rows]),
                      "threads_median": median([s["threads"] for s in rows])}
    return {"cell": name, "kind": "load", "side": side, "lang": SIDES[side][0], "label": SIDES[side][1],
            "started": t0, "busy_before": busy, "busy_after": psutil.cpu_percent(interval=1),
            "ready_s": server.ready_s, "bodies_sha256": bodies, "ramp": ramp, "steady": steady,
            "capped": capped | {"rate": CAP_RATE}, "memory": mem, "samples": server.samples}


def jvm_facts(results: Path) -> dict:
    """What the JVM did before its first answer: classes loaded (and how many came from the AOT cache) and methods
    the JIT compiled, from one extra probe run per JVM side with unified logging to files."""
    out = {}
    for side in ("spring", "spring-aot"):
        argv = SIDES[side][2]
        classes, jit = results / f"facts-{side}-classes.log", results / f"facts-{side}-jit.log"
        logged = [argv[0], f"-Xlog:class+load=info:file={classes.as_posix()}",
                  f"-Xlog:jit+compilation=debug:file={jit.as_posix()}", *argv[1:]]
        pid = results / f"facts-{side}.pid"
        env = {**os.environ, "SERVER_CPUS": SERVER_CPUS, "PROBE_CPUS": CLIENT_CPUS, "PORT": str(PORT)}
        subprocess.check_call([PROBE, "-label", side, "-log", str(results / "facts-probe.jsonl"), "-pidfile", str(pid),
                               "-port", str(PORT), "--", *logged], env=env, creationflags=NO_WINDOW)
        subprocess.check_call([PROBE, "-wait", str(pid)], creationflags=NO_WINDOW)
        lines = classes.read_text(errors="replace").splitlines()
        out[side] = {"classes_loaded": sum(1 for l in lines if "source:" in l),
                     "classes_from_aot_cache": sum(1 for l in lines if "source: shared objects file" in l),
                     # One line per compile task; deoptimisations ("made not entrant") are extra lines, not compiles.
                     "jit_compiled": sum(1 for l in jit.read_text(errors="replace").splitlines()
                                         if "::" in l and "made not entrant" not in l and "made zombie" not in l)}
    return out


def order_for(round_no: int, sides: list[str]) -> list[str]:
    k = (round_no - 1) % len(sides)
    return sides[k:] + sides[:k]


def du(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else path.stat().st_size


def sizes() -> dict:
    """Bytes on disk of what each side needs to run: its own files, and the runtime it needs installed."""
    dotnet_shared = Path("C:/Program Files/dotnet/shared")
    rt = json.loads((BUILD / "cs-jit/orders.runtimeconfig.json").read_text())["runtimeOptions"]
    frameworks = rt.get("frameworks") or [rt["framework"]]
    net_runtime = 0
    for fw in frameworks:
        # The newest installed patch of each framework the JIT build rolls forward to.
        dirs = sorted((dotnet_shared / fw["name"]).glob(fw["version"].rsplit(".", 1)[0] + ".*"))
        net_runtime += du(dirs[-1])
    return {
        "spring_jar": du(BUILD / "orders.jar"),
        "spring_aot_extracted": du(BUILD / "extracted"), "spring_aot_cache": du(BUILD / "orders.aot"),
        "jdk": du(JAVA_HOME), "spring_native_exe": du(BUILD / "orders-native.exe"),
        "cs_jit_app": du(BUILD / "cs-jit"), "dotnet_runtime": net_runtime, "cs_aot_exe": du(BUILD / "cs-aot/orders.exe"),
        "go_exe": du(BUILD / "go-orders.exe"),
    }


def versions() -> dict:
    def cmd(*a: str) -> str:
        try:
            r = subprocess.run(a, check=False, capture_output=True, text=True, creationflags=NO_WINDOW)
            return (r.stdout + r.stderr).strip()
        except OSError as e:
            return f"unavailable: {e}"
    flags = cmd(PY, PIN, SERVER_CPUS, JAVA, "-XX:+PrintFlagsFinal", "-version")
    read = lambda p: (BUILD / p).read_text().strip() if (BUILD / p).exists() else ""
    return {
        "java": cmd(JAVA, "-version"), "graalvm": read("graalvm-version.txt"),
        "java_defaults_on_server_cpus": [l.strip() for l in flags.splitlines() if any(
            k in l for k in (" MaxHeapSize ", " UseG1GC ", " UseSerialGC ", " ActiveProcessorCount ",
                             " ParallelGCThreads ", " CICompilerCount "))],
        "spring_boot": read("spring-boot-version.txt"), "dotnet_sdk": read("dotnet-version.txt"),
        "dotnet_runtimes": cmd("dotnet", "--list-runtimes"),
        "go_binary": cmd("C:/Program Files/Go/bin/go.exe", "version", str(BUILD / "go-orders.exe")),
        "oha": cmd(OHA, "--version"), "hyperfine": cmd(HYPERFINE, "--version"),
        "os": platform.platform(), "cpu": platform.processor(), "ram_bytes": psutil.virtual_memory().total,
        "power_plan": cmd("powercfg", "/getactivescheme"),
    }


def main() -> int:
    results = Path(sys.argv[1])
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    sides = os.environ.get("BENCH_SIDES", ",".join(SIDES)).split(",")
    results.mkdir(parents=True, exist_ok=False)
    (results / "meta.json").write_text(json.dumps(
        {"versions": versions(), "sizes": sizes(), "rounds": rounds, "sides": sides,
         "server_cpus": SERVER_CPUS, "client_cpus": CLIENT_CPUS, "connections": CONNECTIONS,
         "cap_rate": CAP_RATE, "short": SHORT, "start_runs": RUNS,
         "phases_s": {"idle": IDLE_S, "ramp": RAMP_S, "steady": STEADY_S, "capped": CAP_S},
         "started": time.ctime()}, indent=2))
    log("waiting for a quiet machine (up to 4 h)")
    quiet_gate(max_wait=4 * 3600)
    if any(s.startswith("spring") for s in sides) and os.environ.get("BENCH_NO_FACTS") != "1":
        (results / "facts.json").write_text(json.dumps(jvm_facts(results), indent=2))
    all_rows = []
    for r in range(1, rounds + 2):
        record = r <= rounds
        rdir = results / (f"round{r}" if record else "plain")
        rdir.mkdir()
        log(f"round {r} ({'recorded' if record else 'plain'})")
        for side in order_for(r, sides):
            for cell in (start_cell, load_cell):
                res = cell(side, rdir, record)
                res["round"] = r if record else "plain"
                all_rows.append(res)
                if res["kind"] == "start":
                    log(f"  {res['cell']:20s} median {median(res['wall_s']) * 1000:8.1f} ms "
                        f"(probe {median(res['first_ms']):8.1f} ms)")
                else:
                    log(f"  {res['cell']:20s} ready {res['ready_s']:.3f} s  ramp {res['ramp']['rps']:>9,.0f}  "
                        f"steady {res['steady']['rps']:>9,.0f} req/s  idle "
                        f"{res['memory']['idle']['working_set_median'] / 2**20:.0f} MB  capped "
                        f"{res['memory']['capped']['working_set_median'] / 2**20:.0f} MB")
                with open(results / "runs.jsonl", "a") as f:
                    f.write(json.dumps(res) + "\n")
        shas = {json.dumps(x["bodies_sha256"], sort_keys=True) for x in all_rows if x["kind"] == "load"}
        firsts = {x["first_sha256"] for x in all_rows if x["kind"] == "start"}
        if len(shas) != 1 or len(firsts) != 1:
            raise RuntimeError(f"servers answered different bytes: {shas} {firsts}")
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
