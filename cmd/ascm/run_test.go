// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"fmt"
	"strings"
	"testing"

	"github.com/tbhb-dev/ascm/internal/buildmeta"
)

func TestRun(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		args   []string
		code   int
		stdout string
		stderr string
	}{
		{
			name:   "version",
			args:   []string{"--version"},
			stdout: fmt.Sprintf("ascm %s (commit %s, date %s)\n", buildmeta.Version, buildmeta.Commit, buildmeta.Date),
		},
		{name: "short help", args: []string{"-h"}, stdout: "Usage: ascm"},
		{name: "long help", args: []string{"--help"}, stdout: "Usage: ascm"},
		{name: "no arguments", code: 2, stderr: "ascm: expected --version or --help"},
		{
			name:   "version false",
			args:   []string{"--version=false"},
			code:   2,
			stderr: "ascm: expected --version or --help",
		},
		{name: "short help false", args: []string{"-h=false"}, code: 2, stderr: "ascm: expected --version or --help"},
		{name: "flag terminator", args: []string{"--"}, code: 2, stderr: "ascm: expected --version or --help"},
		{name: "unknown flag", args: []string{"--bogus"}, code: 2, stderr: "flag provided but not defined: -bogus"},
		{
			name:   "invalid boolean value",
			args:   []string{"--version=yes"},
			code:   2,
			stderr: `invalid boolean value "yes" for -version`,
		},
		{name: "positional", args: []string{"commit"}, code: 2, stderr: "ascm: expected --version or --help"},
		{
			name:   "version and positional",
			args:   []string{"--version", "commit"},
			code:   2,
			stderr: "ascm: expected --version or --help",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var stdout, stderr bytes.Buffer
			if got := Run(tc.args, &stdout, &stderr); got != tc.code {
				t.Errorf("exit code = %d, want %d", got, tc.code)
			}
			checkOutput(t, "stdout", stdout.String(), tc.stdout)
			checkOutput(t, "stderr", stderr.String(), tc.stderr)
			if tc.code == 2 && !strings.Contains(stderr.String(), "Usage: ascm") {
				t.Errorf("stderr lacks usage: %q", stderr.String())
			}
		})
	}
}

func checkOutput(t *testing.T, writer, got, want string) {
	t.Helper()
	if (want == "" && got != "") || !strings.Contains(got, want) {
		t.Errorf("%s = %q, want %q", writer, got, want)
	}
}
