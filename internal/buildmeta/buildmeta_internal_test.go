// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package buildmeta

import (
	"runtime/debug"
	"slices"
	"testing"
)

func reader(buildInfo *debug.BuildInfo, ok bool) func() (*debug.BuildInfo, bool) {
	return func() (*debug.BuildInfo, bool) { return buildInfo, ok }
}

func TestResolve(t *testing.T) {
	t.Parallel()

	defaults := Info{Version: devVersion, Commit: unknown, Date: unknown}
	full := &debug.BuildInfo{
		Main: debug.Module{Version: "v1.2.3"},
		Settings: []debug.BuildSetting{
			{Key: "vcs.revision", Value: "abc123"},
			{Key: "vcs.time", Value: "2026-01-02T03:04:05Z"},
			{Key: "other", Value: "x"},
		},
	}

	filled := Info{"v1.2.3", "abc123", "2026-01-02T03:04:05Z"}

	// The same settings in reverse, the unrelated one first: each value
	// must come from its own key, not from whichever setting comes first.
	reordered := &debug.BuildInfo{Main: full.Main, Settings: slices.Clone(full.Settings)}
	slices.Reverse(reordered.Settings)

	tests := []struct {
		buildInfo *debug.BuildInfo
		name      string
		in        Info
		want      Info
		ok        bool
	}{
		{nil, "no build info", defaults, defaults, false},
		{full, "fills defaults", defaults, filled, true},
		{full, "keeps injected", Info{"1.0.0", "c", "d"}, Info{"1.0.0", "c", "d"}, true},
		{reordered, "matches keys in any order", defaults, filled, true},
		{
			&debug.BuildInfo{Main: debug.Module{Version: "(devel)"}},
			"devel ignored", defaults, defaults, true,
		},
		{
			&debug.BuildInfo{Settings: []debug.BuildSetting{{Key: "vcs.revision"}, {Key: "vcs.time"}}},
			"empty vcs values ignored", defaults, defaults, true,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			got := resolve(test.in, reader(test.buildInfo, test.ok))
			if got != test.want {
				t.Errorf("resolve() = %+v, want %+v", got, test.want)
			}
		})
	}
}
