#!/bin/sh
set -eu

test "$(dpkg-query -W -f='${Version}' zlib1g)" = \
    '1:1.3.3~academy.git20260904.e3dc0a8-1'
test "$(dpkg-query -W -f='${Version}' libpcre2-8-0)" = \
    '10.48-2~academy1'
test "$(dpkg-query -W -f='${Version}' libxml2)" = \
    '2.15.4+really2.9.14-2.1+deb13u3+academy1'
python -c 'import ctypes, zlib; assert zlib.decompress(zlib.compress(b"academy")) == b"academy"; ctypes.CDLL("libxml2.so.2")'
printf 'academy\n' | grep -P '^academy$'
