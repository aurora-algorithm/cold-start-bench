//go:build windows

package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"syscall"
)

var (
	kernel32                   = syscall.NewLazyDLL("kernel32.dll")
	procSetProcessAffinityMask = kernel32.NewProc("SetProcessAffinityMask")
)

// setMask sets this process's affinity to a list like "0,2,4,6" or "8-15"; "" leaves it alone.
func setMask(cpus string) error {
	if cpus == "" {
		return nil
	}
	var mask uintptr
	for _, part := range strings.Split(cpus, ",") {
		lo, hi, found := strings.Cut(part, "-")
		a, err := strconv.Atoi(lo)
		if err != nil {
			return fmt.Errorf("cpu list %q: %v", cpus, err)
		}
		b := a
		if found {
			if b, err = strconv.Atoi(hi); err != nil {
				return fmt.Errorf("cpu list %q: %v", cpus, err)
			}
		}
		for c := a; c <= b; c++ {
			mask |= 1 << c
		}
	}
	self, _ := syscall.GetCurrentProcess()
	if r, _, err := procSetProcessAffinityMask.Call(uintptr(self), mask); r == 0 {
		return fmt.Errorf("SetProcessAffinityMask(%s): %v", cpus, err)
	}
	return nil
}

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
	const synchronize = 0x00100000
	h, err := syscall.OpenProcess(synchronize, false, uint32(pid))
	if err == nil {
		defer syscall.CloseHandle(h)
		if ev, _ := syscall.WaitForSingleObject(h, 30000); ev != syscall.WAIT_OBJECT_0 {
			return fmt.Errorf("pid %d still running after 30 s", pid)
		}
	}
	return os.Remove(pidPath)
}
