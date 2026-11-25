# Bundled TDL extension

These changes to TDL are licensed under AGPL-3.0, as is upstream TDL. This is a modified version, identified as `0.20.4-relay1`.

Upstream is pinned to commit `9d7d49eef2bcb04da720c26e33598c49c68b9ddd` (v0.20.4). The corresponding GitHub source archive is bundled as `upstream/tdl-0.20.4.tar.gz`, avoiding source-download rate limits during builds. `build.py` verifies its SHA-256 (`072039b13548f6b6e7cc38cc11ddfaeddd310d5797f6af3c44714cb460489b62`) and requires every patch context to match exactly. Dependency versions and checksums remain in upstream `go.mod` / `go.sum`.

The extension adds exclusive receipt files, exact album membership checks, strict errors instead of partial media degradation, and editing of an existing target message. Send receipts match Telegram `random_id` values, not response order or target history. Edits only call `messages.editMessage`, and verify the original sending account when one is recorded.

To produce the corresponding source, use Python 3.13+ to run `python tdl_patch/build.py /path/to/new-empty-directory`. In that directory, Go 1.25.8+ can run `go test -p 1 ./core/forwarder` and `go build -buildvcs=false -p 1 .`. The Dockerfile in the upstream telegram-forwarder-web project contains the Linux amd64/arm64 build commands. The integrated image inherits that build by digest. No Telegram credentials are required for the tests.

Each image also includes the complete modified source under `/usr/share/doc/tdl/source/`, these patch files under `/usr/share/doc/tdl/patch/`, and the upstream license at `/usr/share/doc/tdl/LICENSE`.
