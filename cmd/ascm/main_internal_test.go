// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestRun(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name     string
		wantOut  string
		args     []string
		wantCode int
	}{
		{"version flag", "ascm ", []string{"--version"}, 0},
		{"version subcommand", "ascm ", []string{"version"}, 0},
		{"unknown command", "", []string{"nope"}, 1},
		{"version rejects args", "", []string{"version", "x"}, 1},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			var out, errOut bytes.Buffer

			code := run(test.args, &out, &errOut)
			if code != test.wantCode {
				t.Errorf("code = %d, want %d (stderr %q)", code, test.wantCode, errOut.String())
			}

			missingMeta := test.wantOut != "" && !strings.Contains(out.String(), "(commit ")
			if !strings.HasPrefix(out.String(), test.wantOut) || missingMeta {
				t.Errorf("stdout = %q, want prefix %q with commit/date", out.String(), test.wantOut)
			}
		})
	}
}
