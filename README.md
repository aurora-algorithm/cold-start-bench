# Cold start: Spring Boot on the JVM, with an AOT cache, as a native image, vs ASP.NET Core and Go

Code and per-run results behind the "Spring Boot native image vs JVM: starts 28x faster, serves half as much" Short.

## What is tested
One small API, six ways to build and run it. `GET /orders/{id}` builds one order (10 line items, 645 bytes) from
formulas and returns it as JSON. Every server returns the same bytes, and every cell checks their sha256 against the
other sides.

| Side | Build | Run |
|---|---|---|
| `spring` | Spring Boot 4.1 (Spring MVC on Tomcat, Jackson), `mvn package`, Temurin JDK 25 | `java -jar build/orders.jar`, every JVM setting default |
| `spring-aot` | the same jar, extracted, plus a JDK 25 AOT cache from one training run (`-XX:AOTCacheOutput`, `-Dspring.context.exit=onRefresh`) | `java -XX:AOTCache=build/orders.aot -jar build/extracted/orders.jar` |
| `spring-native` | the same sources, `mvn -Pnative native:compile` on GraalVM CE for JDK 25 (native-image defaults: Serial GC, no PGO) | `build/orders-native.exe` |
| `cs-jit` | ASP.NET Core minimal API (.NET 10, `CreateSlimBuilder`, source-generated System.Text.Json), `dotnet publish` | `build/cs-jit/orders.exe` |
| `cs-aot` | the same code, `dotnet publish -p:PublishAot=true` | `build/cs-aot/orders.exe` |
| `go` | Go `net/http` + `encoding/json`, `go build -trimpath` | `build/go-orders.exe` |

Three tests per side:
1. **Start:** process start to the first `200` on `/orders/1`. `probe/` starts the server, polls the port with 5 ms
   connect attempts, stops the clock at the first answer and kills the server. hyperfine times the probe (`-N`,
   2 warm-ups, 20 runs per round). The figure includes the probe's own start, a few milliseconds, the same on every
   side.
2. **Memory under the same load:** working set while oha sends 5,000 requests a second for 20 s
   (`-q 5000 --latency-correction`), the same rate for every side.
3. **Full speed:** a fresh server, oha with 64 connections and random ids for 20 s right after start, then 20 s more
   once warm (the warm rate below).

## Setup
From `results/i7win-20261110/meta.json`:
- **Machine:** Intel i7-13700F, 32 GB, Windows 11 (build 26200), run natively (no Docker, no WSL), Balanced power plan.
- **Versions:** Temurin JDK 25.0.5 · Spring Boot 4.1.2 · GraalVM CE 25.5.5.0.3 (Java 25.0.5) · .NET SDK 10.0.403,
  ASP.NET Core 10.0.14 · Go 1.27.3 · oha 1.16.0 · hyperfine 2.0.0.
- **Cores:** the server is pinned to logical CPUs 0,2,4,6 (four P-cores, their SMT siblings idle) from its first
  instruction; the probe, hyperfine and oha run on 8-15; the E-cores are left alone.
- **JVM defaults on those four cores:** G1 GC, max heap 8 GB (ergonomic), 3 JIT compiler threads.
- **Runs:** 5 rounds, side order rotating every round, then one "plain" round. Rounds 1-5 record the live hyperfine
  and oha screens (`tools/cast.py`); the plain round runs without recording, to check that recording did not move the
  numbers. Medians below are over rounds 1-5 (start: 100 runs per side; load: 5 runs per side).
- **Quiet machine:** before every cell the script waits until the 16 P-core threads average under 5% CPU (none over
  15%) and the whole machine is under 10%. The reading is saved with every cell in `runs.jsonl`.

## Results
From `results/i7win-20261110/summary.json` (made by `tools/summarize.py` from `runs.jsonl`). MB is MiB.

| Side | Start to first answer, median (min-max) | Working set at 5,000 req/s | Warm req/s (64 conn.) | Warm p99 |
|---|---|---|---|---|
| Spring Boot, JVM | 1,549 ms (1,474-1,653) | 341 MB | 118,051 | 1.22 ms |
| Spring Boot, JVM + AOT cache | 924 ms (859-998) | 841 MB | 120,878 | 1.06 ms |
| Spring Boot, native image | 56.3 ms (54.9-70.7) | 66.8 MB | 55,381 | 5.70 ms |
| ASP.NET Core, JIT | 172 ms (163-203) | 68.3 MB | 235,916 | 2.07 ms |
| ASP.NET Core, Native AOT | 31.8 ms (30.8-34.1) | 36.4 MB | 205,110 | 4.67 ms |
| Go net/http | 15.2 ms (14.7-17.1) | 16.8 MB | 184,875 | 1.35 ms |

The native image starts 27.5 times faster than the plain JVM (1,549 / 56.3 ms); the warm JVM serves 2.1 times as many
requests a second (118,051 / 55,381).

The JVM + AOT cache side commits 653 MB of private memory before its first request (196 MB on the plain JVM;
`idle_private_mb` in `summary.json`). We have not found the cause yet, so the Short leaves it out of the memory chart.

Why the JVM starts slowly, from `results/i7win-20261110/facts.json` (one extra run of each JVM side with
`-Xlog:class+load` and `-Xlog:jit+compilation=debug`, logs beside it): before the first answer the plain JVM loads
7,378 classes and JIT-compiles 4,434 methods; with the AOT cache it loads 7,530 classes, 6,783 of them from the cache.
The native image build report is `results/i7win-20261110/native-build.json` (18,879 reachable types, 88,738 reachable
methods, a 68,411,392-byte executable).

## Reproduce
Windows 11 with Git Bash. Needs Temurin JDK 25, Go, .NET SDK 10, Visual Studio 2022 C++ build tools (`vcvars64.bat`,
for native-image and Native AOT), oha and hyperfine (`winget install hatoo.oha sharkdp.hyperfine`), and Python 3.12
with `psutil` and `pywinpty`. Run from the repo root:

```bash
bash tools/build.sh                                   # downloads GraalVM CE + Maven into tools/, builds into build/
python tools/session.py results/my-run 5              # about 65 minutes on a quiet machine
python tools/summarize.py results/my-run              # writes results/my-run/summary.json
```

`BENCH_JAVA_HOME` points the scripts at another JDK 25. `BENCH_SHORT=1 BENCH_NO_GATE=1 python tools/session.py
results/pilot 1` runs a short pilot with shorter phases and no quiet-machine gate.

Raw data per session: `runs.jsonl` (one line per cell: every hyperfine run, every probe reading, the 0.1 s throughput
bins of the first 20 s, every memory sample every 0.5 s), hyperfine and oha JSON exports per round, the asciinema casts
of the recorded screens, and the server logs.

## Settings people may argue about
- The JVM sides run with every JVM setting at its default: G1 GC and an ergonomic 8 GB max heap on this machine.
- The native image is GraalVM Community with native-image defaults: Serial GC, no profile-guided optimization.
- All three Spring sides run with `--server.tomcat.max-keep-alive-requests=-1`. Tomcat's default closes a connection
  after 100 requests, which at this rate filled Windows' local ports with sockets in TIME_WAIT and made oha fail.
  Go and ASP.NET Core keep connections open by default.
- Spring Boot logs at WARN apart from its "Started ... in" line, ASP.NET Core at Warning; all console output goes to
  the null device during start-up runs.
- The OS file cache is warm (hyperfine's warm-ups), as on a host that already has the image.
- Load generator and server share one machine, on separate cores.
