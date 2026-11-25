"""Derive GHCR tags from the requested release version."""

import os
import re


def metadata(repository, version):
    number = r"(?:0|[1-9][0-9]*)"
    if len(version) > 128 or not re.fullmatch(rf"[1-9][0-9]*\.{number}\.{number}", version):
        raise ValueError("Enter a stable X.Y.Z version starting at 1.0.0 (no v prefix, at most 128 characters)")
    image = "ghcr.io/" + repository.lower()
    return {"version": version, "tags": f"{image}:{version},{image}:latest"}


if __name__ == "__main__":
    result = metadata(os.environ["GITHUB_REPOSITORY"], os.environ["RELEASE_VERSION"])
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in result.items():
            print(f"{key}={value}", file=stream)
