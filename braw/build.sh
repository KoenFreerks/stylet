#!/bin/sh
# Build braw2pipe against the free Blackmagic RAW SDK for Linux.
#   ./build.sh "/path/to/Blackmagic RAW SDK/Linux"
# Then: export BRAW_SDK_LIBS="<that path>/Libraries" and put braw2pipe on PATH
# (or set STYLET_BRAW_HELPER=/full/path/braw2pipe).
set -e
SDK="${1:?usage: build.sh /path/to/Blackmagic\ RAW\ SDK/Linux}"
g++ -O2 -std=c++14 -I"$SDK/Include" braw2pipe.cpp "$SDK/Include/BlackmagicRawAPIDispatch.cpp" \
    -ldl -lpthread -o braw2pipe
echo "built $(pwd)/braw2pipe -- now: export BRAW_SDK_LIBS=\"$SDK/Libraries\""
