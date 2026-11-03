// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package buildmeta exposes build-time information. Release builds inject the
// values through linker flags (Bazel x_defs); go install builds fall back to
// runtime/debug build info.
package buildmeta

import "runtime/debug"

const (
	devVersion = "dev"
	unknown    = "unknown"
)

// Build-time variables, patched by the linker with -X.
//
//nolint:gochecknoglobals // linker -X can only patch package-level vars
var (
	// Version is the semantic version of the build.
	Version = devVersion
	// Commit is the git commit of the build.
	Commit = unknown
	// Date is the commit date of the build.
	Date = unknown
)

// Info bundles build metadata.
type Info struct {
	Version string
	Commit  string
	Date    string
}

// Get returns the build metadata, filling defaults from the runtime build info.
func Get() Info {
	return resolve(Info{Version: Version, Commit: Commit, Date: Date}, debug.ReadBuildInfo)
}

// resolve replaces default values in info with those from readBuildInfo.
func resolve(info Info, readBuildInfo func() (*debug.BuildInfo, bool)) Info {
	buildInfo, ok := readBuildInfo()
	if !ok {
		return info
	}

	if info.Version == devVersion && buildInfo.Main.Version != "" && buildInfo.Main.Version != "(devel)" {
		info.Version = buildInfo.Main.Version
	}

	for _, s := range buildInfo.Settings {
		switch {
		case s.Key == "vcs.revision" && info.Commit == unknown && s.Value != "":
			info.Commit = s.Value
		case s.Key == "vcs.time" && info.Date == unknown && s.Value != "":
			info.Date = s.Value
		}
	}

	return info
}
