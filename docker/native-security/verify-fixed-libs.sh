#!/bin/sh
set -eu

zlib_version="$(dpkg-query -W -f='${Version}' zlib1g)"
test "${zlib_version}" = \
    '1:1.3.dfsg+really1.3.2.1+academy.git20260904.e3dc0a8-1'
test "$(dpkg-query -W -f='${Source}' zlib1g)" = \
    'zlib'
dpkg --compare-versions "${zlib_version}" gt "1:1.3.dfsg+really1.3.1-1"
dpkg --compare-versions "${zlib_version}" lt "1:1.3.dfsg+really1.3.3"
test -z "$(dpkg -L zlib1g | grep -Ei '(py)?minizip' || true)"
test "$(dpkg-query -W -f='${Version}' libpcre2-8-0)" = \
    '10.48-2~academy1'
test "$(dpkg-query -W -f='${Version}' libxml2)" = \
    '2.15.4+really2.9.14-2.1+deb13u3+academy1'
python -c 'import ctypes, zlib; libz = ctypes.CDLL("libz.so.1"); assert not hasattr(libz, "zipOpenNewFileInZip4_64"); assert zlib.decompress(zlib.compress(b"academy")) == b"academy"; ctypes.CDLL("libxml2.so.2")'
printf 'academy\n' | grep -P '^academy$'
