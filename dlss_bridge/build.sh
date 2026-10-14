#!/usr/bin/env bash
# Builds libsacredfire_dlss.so on Linux: build.sh <DLSS SDK folder> [folder with vulkan/vulkan.h]
# The Linux counterpart of build.bat: the same bridge, linked with the SDK's libnvsdk_ngx.a. Put it
# next to the game binary (out/ or the package's bin/) with the SDK's
# lib/Linux_x86_64/rel/libnvidia-ngx-dlss.so.<version>; build.sh --install does that for out/.
set -euo pipefail
cd -- "$(dirname -- "$0")"
sdk=${1:?usage: build.sh <DLSS SDK folder> [Vulkan include folder] [--install]}
vkinc=${2:-}
[[ $vkinc == --install ]] && vkinc=
includes=(-I"$sdk/include")
if [[ -n $vkinc ]]; then includes+=(-I"$vkinc"); else read -r -a flags <<< "$(pkg-config --cflags vulkan)"; includes+=("${flags[@]}"); fi
"${CXX:-g++}" -std=c++20 -O2 -fPIC -shared -fvisibility=hidden -Wall -Wno-unused-parameter "${includes[@]}" \
    shadps4_dlss_bridge.cpp "$sdk/lib/Linux_x86_64/libnvsdk_ngx.a" -ldl -Wl,--no-undefined -Wl,-z,defs \
    -static-libstdc++ -static-libgcc -o libsacredfire_dlss.so
echo "Built $PWD/libsacredfire_dlss.so"
if [[ " $* " == *" --install "* ]]; then
    runtime=$(ls "$sdk"/lib/Linux_x86_64/rel/libnvidia-ngx-dlss.so.* | head -1)
    mkdir -p ../out/licenses
    install -m755 libsacredfire_dlss.so ../out/
    install -m755 "$runtime" ../out/
    install -m644 "$sdk/LICENSE.txt" ../out/licenses/NVIDIA-DLSS-SDK-LICENSE.txt
    install -m644 LICENSE.txt ../out/licenses/dlss-bridge-LICENSE.txt
    echo "Installed into out/: libsacredfire_dlss.so, $(basename "$runtime"), licenses/"
fi
