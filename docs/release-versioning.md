# Releases and versions

## Version scheme

The hub uses calendar versions (`YYYY.M.PATCH`, for example `2026.12.0`). Development versions
end in `-dev` (`2026.12.0-dev`) and are never published.

The version must be the same in these files; `scripts/ci/check-versions.sh` checks it in CI:

- `halumar/config.yaml` (`version:`, what the Supervisor reads),
- `halumar/rootfs/opt/hub/version.py`,
- `frontend/package.json` and `frontend/package-lock.json`,
- the top section of `halumar/CHANGELOG.md`.

Hub and integration of the same version belong together. The protocol between hub, integration
and apps has its own version in the [contract](https://github.com/Halumar/contracts); the hub
pins the contract to the tag in [`.github/contracts-ref`](../.github/contracts-ref). The hub
accepts apps of its protocol version and the two previous ones and asks older apps to update.

## Branches

- `develop`: day-to-day work; pull requests go here.
- `main`: what Home Assistant users see. Home Assistant reads `halumar/config.yaml` from the
  default branch of the repository, so a version on `main` is announced to every user at once.

## How a release is made

1. Set the release version (without `-dev`) in the files above and add a
   `## <version>` section to `halumar/CHANGELOG.md`.
2. Merge to `main` through a pull request whose checks are green.
3. The push to `main` starts the workflow `build.yaml`. It runs the backend and frontend gates,
   reads `version:` from `halumar/config.yaml`, checks that the version has a changelog section
   and no tag yet, builds the admin page once, builds the images for `amd64` and `aarch64` on
   native GitHub-hosted runners, signs them with cosign, pushes them to `ghcr.io/halumar/hub` as
   a multi-arch manifest, and only then creates the git tag and the GitHub release.

Home Assistant reads `version:` on `main` at once, before the images exist: between the merge and
the end of `build.yaml` an update or new install of that version fails. The tag is created only
after the images are pushed; wait for the tag before updating. There is no manual tag step: the
tag is a receipt that the images exist. If the build fails, no tag appears and the next push to
`main` retries. A push to `main` whose version is already tagged, or ends in `-dev`, publishes
nothing.

Pushing a tag `YYYY.M.PATCH` by hand rebuilds and publishes that version; the tag must match
`version:` in `halumar/config.yaml`. Such a run also moves `latest` and creates no GitHub release
(`tag-release` runs only on `main`).

`ci.yml` and `frontend.yml` run on pull requests and on `develop`. On `main` they run as part of
`build.yaml`, which calls both before it publishes.

The details of the pipeline are in [halumar/ARCHITECTURE.md, section 7](../halumar/ARCHITECTURE.md#7-cicd-and-release).

## Changelog

`halumar/CHANGELOG.md` is the only changelog: Home Assistant shows it for the app, and the
release workflow takes the release notes from its section of the version. Entries that need
attention before updating are marked **Breaking**.
