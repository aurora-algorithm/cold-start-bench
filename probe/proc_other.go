//go:build !windows

package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// setMask is a no-op off Windows (the Mac smoke test).
func setMask(string) error { return nil }

func waitFor(pidPath string) error {
	raw, err := os.ReadFile(pidPath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil {
		return err
	}
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(5 * time.Millisecond) {
		if syscall.Kill(pid, 0) != nil {
			return os.Remove(pidPath)
		}
	}
	return fmt.Errorf("pid %d still running", pid)
}
