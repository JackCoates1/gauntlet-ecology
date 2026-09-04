#!/bin/bash
# Copy the cloud image's existing Python runtime into the read-only sandbox rootfs.
# This runs only while provisioning the dedicated, networkless runner VM.  It does
# not install packages or add a network path to the guest.
set -Eeuo pipefail

guest_root=$1
sandbox_root=$2

copy_path() {
  local relative source destination
  relative=$1
  source="$guest_root$relative"
  destination="$sandbox_root$relative"
  [[ -e "$source" ]] || return 0
  mkdir -p "$(dirname "$destination")"
  cp -a "$source" "$destination"
}

copy_path /usr/bin/python3
copy_path /usr/bin/python3.12
copy_path /usr/lib/python3.12
mkdir -p "$sandbox_root/lib64"
cp -a "$guest_root/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" "$sandbox_root/lib64/ld-linux-x86-64.so.2"
for source in \
  "$guest_root"/lib/x86_64-linux-gnu/ld-linux-x86-64.so.* \
  "$guest_root"/lib/x86_64-linux-gnu/lib{c,m,z,expat,bz2,lzma,ffi,sqlite3,ssl,crypto,readline,ncursesw,tinfo}.so* \
  "$guest_root"/usr/lib/x86_64-linux-gnu/libpython3.12.so*; do
  [[ -e "$source" ]] || continue
  copy_path "${source#"$guest_root"}"
done

chroot "$sandbox_root" /usr/bin/python3 -I -c 'import gzip, json, signal, uuid'
