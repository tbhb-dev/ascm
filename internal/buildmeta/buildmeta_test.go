// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package buildmeta_test

import (
	"testing"

	"github.com/tbhb-dev/ascm/internal/buildmeta"
)

func TestGetReturnsPackageVars(t *testing.T) {
	t.Parallel()
	got := buildmeta.Get()
	want := buildmeta.Info{Version: buildmeta.Version, Commit: buildmeta.Commit, Date: buildmeta.Date}
	if got != want {
		t.Errorf("Get() = %#v, want %#v", got, want)
	}
}
