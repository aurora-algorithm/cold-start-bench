// probe starts a server and measures the time from process start to its first successful HTTP answer.
//
//	probe -log runs.jsonl -pidfile server.pid -- SERVER ARGV...    start, poll, log, kill the server, exit
//	probe -wait server.pid                                           wait until that server process has exited
//
// hyperfine times the first form: probe's own start, the server's start-up and its first GET /orders/1. The server
// gets SERVER_CPUS as its affinity mask from its first instruction (a child inherits its parent's mask, so probe
// sets its own mask to the server's for the moment of the spawn), and probe itself polls from PROBE_CPUS. probe
// kills the server and exits without waiting for it to finish dying; "-wait" (hyperfine's --prepare) waits for that
// before the next run starts.
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"time"
)

type record struct {
	Label     string  `json:"label"`
	ListenMs  float64 `json:"listen_ms"`
	FirstMs   float64 `json:"first_ms"`
	Status    int     `json:"status"`
	BodySha   string  `json:"body_sha256"`
	BodyBytes int     `json:"body_bytes"`
	Attempts  int     `json:"attempts"`
	Pid       int     `json:"pid"`
	Unix      float64 `json:"unix"`
}

func main() {
	logPath := flag.String("log", "", "append one JSON line per run here")
	pidPath := flag.String("pidfile", "", "write the server's pid here (for -wait)")
	waitPid := flag.String("wait", "", "wait until the process in this pid file has exited, then return")
	label := flag.String("label", "", "name written into the log line")
	port := flag.Int("port", 18080, "the server's port on 127.0.0.1")
	path := flag.String("path", "/orders/1", "the request that counts as the first successful answer")
	timeout := flag.Duration("timeout", 60*time.Second, "give up after this long")
	flag.Parse()
	if *waitPid != "" {
		exitOn(waitFor(*waitPid))
		return
	}
	if flag.NArg() == 0 {
		exitOn(fmt.Errorf("no server command"))
	}
	exitOn(run(*label, *logPath, *pidPath, *port, *path, *timeout, flag.Args()))
}

func exitOn(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "probe:", err)
		os.Exit(1)
	}
}

func run(label, logPath, pidPath string, port int, path string, timeout time.Duration, argv []string) error {
	addr := "127.0.0.1:" + strconv.Itoa(port)
	if c, err := net.DialTimeout("tcp", addr, 20*time.Millisecond); err == nil {
		c.Close()
		return fmt.Errorf("%s already answers: an earlier server is still running", addr)
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr = nil, nil // the null device: console output would cost every side differently
	if err := setMask(os.Getenv("SERVER_CPUS")); err != nil {
		return err
	}
	start := time.Now()
	err := cmd.Start()
	if maskErr := setMask(os.Getenv("PROBE_CPUS")); maskErr != nil && err == nil {
		err = maskErr
	}
	if err != nil {
		return err
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	defer cmd.Process.Kill()

	rec := record{Label: label, Pid: cmd.Process.Pid}
	if pidPath != "" {
		if err := os.WriteFile(pidPath, []byte(strconv.Itoa(rec.Pid)), 0o644); err != nil {
			return err
		}
	}
	for listened := false; ; {
		select {
		case err := <-exited:
			return fmt.Errorf("server exited during start-up: %v", err)
		default:
		}
		if time.Since(start) > timeout {
			return fmt.Errorf("no answer within %s", timeout)
		}
		rec.Attempts++
		// Windows holds a refused localhost connect for about half a second, so a short dial timeout keeps the
		// poll near 5 ms instead of 500 ms steps.
		conn, err := net.DialTimeout("tcp", addr, 5*time.Millisecond)
		if err != nil {
			continue
		}
		if !listened {
			rec.ListenMs = ms(time.Since(start))
			listened = true
		}
		status, body, err := get(conn, path)
		if err != nil || status != http.StatusOK {
			time.Sleep(time.Millisecond)
			continue
		}
		rec.FirstMs = ms(time.Since(start))
		rec.Status = status
		sum := sha256.Sum256(body)
		rec.BodySha, rec.BodyBytes = hex.EncodeToString(sum[:]), len(body)
		break
	}
	rec.Unix = float64(time.Now().UnixMicro()) / 1e6
	if logPath == "" {
		return json.NewEncoder(os.Stdout).Encode(rec)
	}
	f, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	return json.NewEncoder(f).Encode(rec)
}

func get(conn net.Conn, path string) (int, []byte, error) {
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(30 * time.Second))
	req, err := http.NewRequest(http.MethodGet, "http://"+conn.RemoteAddr().String()+path, nil)
	if err != nil {
		return 0, nil, err
	}
	req.Close = true
	if err := req.Write(conn); err != nil {
		return 0, nil, err
	}
	resp, err := http.ReadResponse(bufio.NewReader(conn), req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return resp.StatusCode, body, err
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }
