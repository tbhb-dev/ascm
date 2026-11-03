// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"strings"
	"testing"
)

// mainEnv makes the test binary run main instead of the tests, so a test
// can run the command as a process and see its exit status.
const mainEnv = "ASCM_TEST_RUN_MAIN"

// mainReturned is the exit status of a main that returns instead of
// exiting. Running the tests in that process would start it again.
const mainReturned = 3

func TestMain(m *testing.M) {
	if os.Getenv(mainEnv) == "1" {
		main()
		os.Exit(mainReturned)
	}

	os.Exit(m.Run())
}

func TestRun(t *testing.T) {
	t.Parallel()

	version := "ascm " + versionString() + "\n"

	tests := []struct {
		name     string
		wantOut  string
		wantErr  string
		args     []string
		wantCode int
	}{
		{"version flag", version, "", []string{"--version"}, 0},
		{"version subcommand", version, "", []string{"version"}, 0},
		{"unknown command", "", `Error: unknown command "nope" for "ascm"`, []string{"nope"}, 1},
		{"version rejects args", "", `Error: unknown command "x" for "ascm version"`, []string{"version", "x"}, 1},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var out, errOut bytes.Buffer

			code := run(test.args, &out, &errOut)
			if code != test.wantCode {
				t.Errorf("code = %d, want %d (stderr %q)", code, test.wantCode, errOut.String())
			}

			if out.String() != test.wantOut {
				t.Errorf("stdout = %q, want %q", out.String(), test.wantOut)
			}

			if !strings.HasPrefix(errOut.String(), test.wantErr) || (test.wantErr == "") != (errOut.Len() == 0) {
				t.Errorf("stderr = %q, want prefix %q", errOut.String(), test.wantErr)
			}
		})
	}
}

func TestVersionHasCommitAndDate(t *testing.T) {
	t.Parallel()

	if v := versionString(); !strings.Contains(v, " (commit ") || !strings.Contains(v, ", date ") {
		t.Errorf("versionString() = %q, want commit and date", v)
	}
}

func TestMainExitStatus(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name     string
		args     []string
		wantCode int
	}{
		{"success", []string{"--version"}, 0},
		{"failure", []string{"nope"}, 1},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var out bytes.Buffer

			// The test binary itself, which runs main in the environment below.
			cmd := exec.CommandContext(t.Context(), os.Args[0], test.args...) //nolint:gosec // see above
			cmd.Stdout = &out

			cmd.Env = append(os.Environ(), mainEnv+"=1")

			err := cmd.Run()

			code := 0
			if exitErr, ok := errors.AsType[*exec.ExitError](err); ok {
				code = exitErr.ExitCode()
			} else if err != nil {
				t.Fatal(err)
			}

			if code != test.wantCode {
				t.Errorf("exit status = %d, want %d", code, test.wantCode)
			}

			if test.wantCode == 0 && !strings.HasPrefix(out.String(), "ascm ") {
				t.Errorf("stdout = %q, want the version", out.String())
			}
		})
	}
}
