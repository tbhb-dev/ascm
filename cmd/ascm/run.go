// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"flag"
	"fmt"
	"io"

	"github.com/tbhb-dev/ascm/internal/buildmeta"
)

const usageExit = 2

// Run prints usage or build metadata and returns a process exit code.
func Run(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("ascm", flag.ContinueOnError)
	flags.SetOutput(stderr)
	version := flags.Bool("version", false, "Print version, commit and date")
	help := flags.Bool("help", false, "Print usage")
	shortHelp := flags.Bool("h", false, "Print usage")
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "Usage: ascm [--version | -h | --help]")
		flags.PrintDefaults()
	}
	if err := flags.Parse(args); err != nil {
		return usageExit
	}
	if flags.NArg() != 0 || len(args) == 0 {
		fmt.Fprintln(stderr, "ascm: expected --version or --help")
		flags.Usage()
		return usageExit
	}
	if *help || *shortHelp {
		flags.SetOutput(stdout)
		flags.Usage()
		return 0
	}
	if *version {
		info := buildmeta.Get()
		fmt.Fprintf(stdout, "ascm %s (commit %s, date %s)\n", info.Version, info.Commit, info.Date)
	}
	return 0
}
