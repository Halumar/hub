"""The app image's base: pinned by digest, with the Python the wheels are for.

Every pin in requirements.txt is installed as a musl wheel (``--only-binary``);
a base image with another Python minor fails the build or, worse, picks other
wheels. Alpine keeps only the newest build of a package per branch and ships
security fixes as new patch versions, so apk packages are pinned with ``~`` at
most to the minor version (python3~3.12), never to a patch or build.
"""

import os
import re

ADDON = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts: str) -> str:
    with open(os.path.join(ADDON, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestBaseImage:
    def test_base_pinned_by_tag_and_digest(self):
        dockerfile = _read("Dockerfile")
        assert re.search(
            r"^FROM ghcr\.io/hassio-addons/base:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$",
            dockerfile,
            re.M,
        )

    def test_python_minor_matches_the_wheels(self):
        dockerfile = _read("Dockerfile")
        requirements = _read("rootfs", "opt", "hub", "requirements.txt")
        minor = re.search(r"CPython (3\.\d+)", requirements).group(1)
        assert f"python3~{minor}" in dockerfile.split()

    def test_apk_packages_pinned(self):
        dockerfile = _read("Dockerfile")
        block = re.search(r"apk add --no-cache \\\n(.*?)\n\n", dockerfile, re.S).group(
            1
        )
        packages = [p for p in block.replace("\\", " ").split() if p]
        assert packages and all("~" in p for p in packages), packages
        for pin in packages:
            version = pin.split("~", 1)[1]
            # major or major.minor: a patch-level pin breaks with the next
            # security fix of the Alpine branch.
            assert re.fullmatch(r"\d+(\.\d+)?", version), pin
