#!/usr/bin/env bash
# Install pinned upstream binaries into this checkout only.
set -euo pipefail
cd "$(dirname "$0")/.."
source versions.env
case "$(uname -s)" in Linux) os=linux;; Darwin) os=darwin;; *) exit 1;; esac
case "$(uname -m)" in x86_64) arch=amd64;; arm64|aarch64) arch=arm64;; *) exit 1;; esac
mkdir -p .bin
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
verify() {
  python3 - "$1" "$2" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
expected = pathlib.Path(sys.argv[2]).read_text().split()[0]
actual = hashlib.sha256(p.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"Checksum mismatch: {p.name}")
PY
}
curl -fsSL "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-${os}-${arch}" -o "$tmp/kind"
curl -fsSL "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-${os}-${arch}.sha256sum" -o "$tmp/kind.sha256"
verify "$tmp/kind" "$tmp/kind.sha256"
install -m 755 "$tmp/kind" .bin/kind
curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl" -o "$tmp/kubectl"
curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl.sha256" -o "$tmp/kubectl.sha256"
verify "$tmp/kubectl" "$tmp/kubectl.sha256"
install -m 755 "$tmp/kubectl" .bin/kubectl
helm_archive="helm-v${HELM_VERSION}-${os}-${arch}.tar.gz"
curl -fsSL "https://get.helm.sh/${helm_archive}" -o "$tmp/helm.tgz"
curl -fsSL "https://get.helm.sh/${helm_archive}.sha256sum" -o "$tmp/helm.sha256"
verify "$tmp/helm.tgz" "$tmp/helm.sha256"
tar -xzf "$tmp/helm.tgz" -C "$tmp"
install -m 755 "$tmp/${os}-${arch}/helm" .bin/helm
gh_archive="gh_${GH_VERSION}_${os}_${arch}.tar.gz"
if [[ "$os" == darwin ]]; then gh_archive="gh_${GH_VERSION}_macOS_${arch}.zip"; fi
curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${gh_archive}" -o "$tmp/$gh_archive"
curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt" -o "$tmp/gh-checksums"
python3 - "$tmp/$gh_archive" "$tmp/gh-checksums" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
checks = dict((line.split()[1], line.split()[0]) for line in pathlib.Path(sys.argv[2]).read_text().splitlines())
if hashlib.sha256(p.read_bytes()).hexdigest() != checks[p.name]:
    raise SystemExit('gh checksum mismatch')
PY
if [[ "$os" == darwin ]]; then unzip -q "$tmp/$gh_archive" -d "$tmp"; else tar -xzf "$tmp/$gh_archive" -C "$tmp"; fi
gh_dir="gh_${GH_VERSION}_${os}_${arch}"
if [[ "$os" == darwin ]]; then gh_dir="gh_${GH_VERSION}_macOS_${arch}"; fi
install -m 755 "$tmp/$gh_dir/bin/gh" .bin/gh
echo 'Official tools installed into .bin/; Docker and Python 3.9+ must already be available.'
