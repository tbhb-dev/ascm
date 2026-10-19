// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import "os"

func main() {
	os.Exit(Run(os.Args[1:], os.Stdout, os.Stderr))
}
