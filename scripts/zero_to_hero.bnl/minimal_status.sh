#!/bin/bash

# Minimal E2SAR load balancer reservation list script

#

# Usage:

# EJFAT_URI="ejfats://token@host:port/..." ./minimal_list.sh

#

# EJFAT_URI must be the ADMIN URI.

#

# Lists load balancer reservations associated with the admin URI.

set -euo pipefail

# E2SAR_IMAGE="${E2SAR_IMAGE:-ibaldin/e2sar:0.3.2}"
E2SAR_IMAGE="${E2SAR_IMAGE:-docker://ibaldin/e2sar:0.3.2}"

# Parse command-line arguments

while [[ $# -gt 0 ]]; do
case "$1" in
--image)
E2SAR_IMAGE="$2"
shift 2
;;
*)
echo "ERROR: Unknown argument: $1"
echo "Usage: $0 [--image IMAGE]"
exit 1
;;
esac
done

# Validate admin URI

if [[ -z "${EJFAT_URI:-}" ]]; then
echo "ERROR: EJFAT_URI is required"
echo "Set the ADMIN URI via the EJFAT_URI environment variable"
exit 1
fi

EJFAT_URI_REDACTED=$(echo "$EJFAT_URI" |
sed -E 's|(://)(.{4})[^@]*(.{4})@|\1\2---\3@|')

echo "Admin EJFAT_URI: $EJFAT_URI_REDACTED"
echo "Listing reservations..."

export EJFAT_URI

apptainer exec --env EJFAT_URI="$EJFAT_URI" "$E2SAR_IMAGE" lbadm --overview

