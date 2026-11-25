"""Derive GHCR tags from the validated source version and GitHub event."""

import os
import re
from pathlib import Path


def metadata(ref, repository, version):
    number = r"(?:0|[1-9][0-9]*)"
    if not re.fullmatch(rf"[1-9][0-9]*\.{number}\.{number}", version):
        raise ValueError("VERSION must be a stable X.Y.Z starting at 1.0.0")
    tags = []
    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        if tag != "v" + version:
            raise ValueError(f"Release tag must equal v{version} from VERSION")
        image = "ghcr.io/" + repository.lower()
        tags = [f"{image}:{version}", f"{image}:latest"]
    return {"version": version, "tags": ",".join(tags)}


if __name__ == "__main__":
    result = metadata(
        os.environ["GITHUB_REF"], os.environ["GITHUB_REPOSITORY"],
        (Path(__file__).resolve().parents[2] / "VERSION").read_text("ascii").strip(),
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for key, value in result.items():
            print(f"{key}={value}", file=stream)
