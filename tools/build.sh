#!/usr/bin/env bash
# Builds every server the session runs into build/, natively on Windows (Git Bash), from C:/bench/cold-start:
#   build/orders.jar                 Spring Boot fat jar (Temurin JDK 25, Maven 3.9.11 unpacked into tools/)
#   build/extracted/, orders.aot     the extracted jar and its JDK 25 AOT cache (Spring Boot's documented recipe)
#   build/orders-native.exe          the same app as a GraalVM native image (GraalVM CE for JDK 25, MSVC from vcvars64)
#   build/cs-jit/, build/cs-aot/     the ASP.NET Core API, JIT build and Native AOT build (.NET SDK 10)
#   build/go-orders.exe, probe.exe   the Go server and the start-up probe
# Every download and cache (GraalVM, Maven, Maven repository, NuGet, Go) stays under this folder.
# The native builds use every core for minutes; don't run a session at the same time.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd -W 2>/dev/null || pwd)
MAVEN_VERSION=3.9.11
MAVEN_SHA512=03e2d65d4483a3396980629f260e25cac0d8b6f7f2791e4dc20bc83f9514db8d0f05b0479e699a5f34679250c49c8e52e961262ded468a20de0be254d8207076
GRAAL_TAG=graal-25.5.5.0.3
GRAAL_ZIP=graalvm-community-jdk-25i5-25.0.5.0.3_windows-x64_bin.zip
JAVA_HOME=${BENCH_JAVA_HOME:-$(ls -d "/c/Program Files/Eclipse Adoptium"/jdk-25* | tail -1)}
GO=${GO:-"/c/Program Files/Go/bin/go.exe"}
export JAVA_HOME GOCACHE="$ROOT/.cache/go-build" GOMODCACHE="$ROOT/.cache/go-mod" GOPATH="$ROOT/.cache/go" \
  GOTOOLCHAIN=local GOFLAGS=-modcacherw NUGET_PACKAGES="$ROOT/.cache/nuget" DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  DOTNET_NOLOGO=1 DOTNET_CLI_HOME="$ROOT/.cache/dotnet-home"
mkdir -p build tools .cache
step() { echo "== $(date +%H:%M:%S) $*"; }

step go
(cd go && "$GO" build -trimpath -o ../build/go-orders.exe .)
(cd probe && "$GO" build -trimpath -o ../build/probe.exe .)

MVN="tools/apache-maven-$MAVEN_VERSION/bin/mvn"
if [ ! -x "$MVN" ]; then
  step "download maven $MAVEN_VERSION"
  [ -f tools/maven.zip ] || curl -fsSL -o tools/maven.zip "https://archive.apache.org/dist/maven/maven-3/$MAVEN_VERSION/binaries/apache-maven-$MAVEN_VERSION-bin.zip"
  echo "$MAVEN_SHA512  tools/maven.zip" | sha512sum -c -
  unzip -q tools/maven.zip -d tools && rm tools/maven.zip
fi
GRAAL_HOME=$(ls -d tools/graalvm-community-*/ 2>/dev/null | tail -1 || true); GRAAL_HOME=${GRAAL_HOME%/}
if [ -z "$GRAAL_HOME" ]; then
  step "download $GRAAL_ZIP"
  base="https://github.com/graalvm/graalvm-ce-builds/releases/download/$GRAAL_TAG"
  [ -f "tools/$GRAAL_ZIP" ] || curl -fsSL -o "tools/$GRAAL_ZIP" "$base/$GRAAL_ZIP"
  want=$(curl -fsSL "$base/$GRAAL_ZIP.sha256" | tr -d '\r\n ' | cut -c1-64)
  echo "$want  tools/$GRAAL_ZIP" | sha256sum -c -
  unzip -q "tools/$GRAAL_ZIP" -d tools && rm "tools/$GRAAL_ZIP"
  GRAAL_HOME=$(ls -d tools/graalvm-community-*/ | tail -1); GRAAL_HOME=${GRAAL_HOME%/}
fi

step "spring boot jar (Temurin)"
"$JAVA_HOME/bin/java" -version
(cd java && "../$MVN" -B -q -Dmaven.repo.local="$ROOT/.cache/m2" clean package -DskipTests)
cp java/target/orders.jar build/
unzip -p build/orders.jar META-INF/MANIFEST.MF | tr -d '\r' | grep '^Spring-Boot-Version' > build/spring-boot-version.txt
rm -rf build/extracted build/orders.aot
"$JAVA_HOME/bin/java" -Djarmode=tools -jar build/orders.jar extract --destination build/extracted
# The training run starts the app and exits once the context is up (Spring Boot's AOT-cache recipe, JEP 514/515).
PORT=18089 "$JAVA_HOME/bin/java" -XX:AOTCacheOutput=build/orders.aot -Dspring.context.exit=onRefresh \
  -jar build/extracted/orders.jar > build/aot-training.log 2>&1

step "spring boot native image (GraalVM, MSVC)"
GRAAL_WIN=$(cd "$GRAAL_HOME" && pwd -W)
VCVARS="C:\\Program Files\\Microsoft Visual Studio\\2022\\Community\\VC\\Auxiliary\\Build\\vcvars64.bat"
cat > build/native.cmd <<CMD
@echo off
call "$VCVARS" >nul || exit /b 1
set "JAVA_HOME=$GRAAL_WIN"
set "GRAALVM_HOME=$GRAAL_WIN"
cd java
call "..\\tools\\apache-maven-$MAVEN_VERSION\\bin\\mvn.cmd" -B -q -Dmaven.repo.local="$ROOT/.cache/m2" -Pnative clean native:compile -DskipTests || exit /b 1
CMD
cmd //c "build\\native.cmd"
cp java/target/orders-native.exe build/
cp java/target/native-build.json build/ 2>/dev/null || find java -name native-build.json -exec cp {} build/ \;
"$GRAAL_HOME/bin/java" -version 2>&1 | tee build/graalvm-version.txt

step "asp.net core jit + native aot"
rm -rf build/cs-jit build/cs-aot csharp/Api/bin csharp/Api/obj
dotnet publish csharp/Api/Api.csproj -c Release -o build/cs-jit --nologo -v quiet
rm -rf csharp/Api/bin csharp/Api/obj
cat > build/aot.cmd <<CMD
@echo off
call "$VCVARS" >nul || exit /b 1
dotnet publish csharp\\Api\\Api.csproj -c Release -o build\\cs-aot -p:PublishAot=true --nologo -v quiet || exit /b 1
CMD
cmd //c "build\\aot.cmd"
dotnet --version > build/dotnet-version.txt
ls -la build build/cs-aot
step done
