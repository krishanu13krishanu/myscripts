#!/bin/bash

# MongoDB hosts (passwords removed)
declare -A passwords

#put list here in below format
#passwords["e12763ch3ctm01"]="xyc"
#passwords["e12457ch3ctm01"]="fdgp"


for host in "${!passwords[@]}"; do
  echo "=== $host ==="
  bytes=$(mongosh "mongodb://$host:27017/iman" -u "user" -p "${passwords[$host]}" --quiet --eval "db.stats().dataSize")
  gb=$(awk "BEGIN {printf \"%.2f\", $bytes/1024/1024/1024}")
  echo "$gb GB"
done

echo "Done!"
