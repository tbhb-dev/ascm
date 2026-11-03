// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package buildmeta_test

import (
	"testing"

	"go.tbhb.dev/ascm/internal/buildmeta"
)

func TestGetNonEmpty(t *testing.T) {
	t.Parallel()

	got := buildmeta.Get()
	if got.Version == "" || got.Commit == "" || got.Date == "" {
		t.Errorf("Get() returned empty field: %+v", got)
	}
}
