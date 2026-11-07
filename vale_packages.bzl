# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""The pinned Vale style packages (MODULE.bazel), for //:vale_test and
//:vale_sync."""

VALE_PACKAGES = {
    "Google": "@vale_google//file",
    "ai-tells": "@vale_ai_tells//file",
    "ai-tells-experimental": "@vale_ai_tells_experimental//file",
    "proselint": "@vale_proselint//file",
    "write-good": "@vale_write_good//file",
}
