// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package buildmeta exposes build-time information injected via linker flags, with
// a fallback that consults the runtime/debug build info when no linker flags apply
// (such as go install directly from source).
package buildmeta

import "runtime/debug"

// devFallback holds the placeholder version stamped when no linker flags
// and no runtime build info apply. The resolver treats it as a
// signal to consult runtime/debug rather than as a real version.
const devFallback = "DEV"

// Build-time variables. The mise build task sets these via
// -ldflags "-X github.com/tbhb-dev/ascm/internal/buildmeta.<Var>=<value>"
// at link time. Builds without linker flags use the defaults below.
//
//nolint:gochecknoglobals // linker flag -X can only patch package-level vars
var (
	// Version holds the semantic version of the build. Set via linker flags, or
	// resolved from the runtime/debug build info at init time.
	Version = devFallback

	// Commit holds the short git commit hash of the build.
	Commit = ""

	// Date holds the build date in calendar form, like 2026-05-26.
	Date = ""
)

// Info bundles build metadata for callers that want a struct rather than
// the package-level vars.
type Info struct {
	Version string
	Commit  string
	Date    string
}

// Get returns the current build metadata.
func Get() Info {
	return Info{Version: Version, Commit: Commit, Date: Date}
}

//nolint:gochecknoinits // runtime/debug fallback runs once at process start
func init() {
	Version = resolveVersion(Version, debug.ReadBuildInfo)
}

// resolveVersion produces the effective package Version from the
// linker-supplied value and a callable that reads the runtime
// build info. When linker flags pinned a non-default version, that
// value wins. With the placeholder fallback in place, the runtime
// build info supplies a module-derived version, except when the
// reader reports the development placeholder. That placeholder leaves
// the placeholder untouched so callers always see a non-empty
// Version.
//
// The function takes readBuildInfo as a parameter so tests can
// drive each branch directly without spawning a child process.
func resolveVersion(ldflagsVersion string, readBuildInfo func() (*debug.BuildInfo, bool)) string {
	if ldflagsVersion != devFallback {
		return ldflagsVersion
	}
	info, ok := readBuildInfo()
	if !ok || info.Main.Version == "(devel)" {
		return ldflagsVersion
	}
	return info.Main.Version
}
