#!/bin/sh
# Remove generated media after users have had time to download it.
set -eu
root=/opt/video-parser
for directory in downloads cache static/videos static/images; do
  target="$root/$directory"
  if [ -d "$target" ]; then
    find "$target" -type f -mmin +2880 -delete
    find "$target" -mindepth 1 -type d -empty -delete 2>/dev/null || true
  fi
done
