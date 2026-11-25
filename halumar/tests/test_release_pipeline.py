"""The release pipeline can run and builds what it claims to build.

- Every job runs on a GitHub-hosted runner. Image jobs run on native Linux
  runners, one per architecture: home-assistant/builder builds for the runner's
  own architecture.
- Image names are lowercase (owner "Halumar"; Docker rejects uppercase).
- Releases ship a bundle built from frontend/src, not the checked-in copy, and the
  npm audit covers the packages that end up in the bundle.

The workflows are read from the repository's .github directory.
"""

import difflib
import json
import os
import re
import shutil
import subprocess

import pytest
import yaml

HERE = os.path.dirname(__file__)
ADDON = os.path.join(HERE, "..")
REPO = os.path.join(ADDON, "..")
_PUBLIC = os.path.join(REPO, ".github", "public", "workflows")
_OWN = os.path.join(REPO, ".github", "workflows")
WORKFLOWS = _PUBLIC if os.path.isdir(_PUBLIC) else _OWN
ACTIONS = os.path.join(os.path.dirname(WORKFLOWS), "actions")
AUDIT_SCRIPT = os.path.join(REPO, "scripts", "ci", "npm-audit-bundle.mjs")


def _read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _workflow(name: str) -> dict:
    return yaml.safe_load(_read(WORKFLOWS, name))


def _uses(job: dict, action: str) -> list[dict]:
    return [s for s in job.get("steps", []) if s.get("uses", "").startswith(action)]


BUILD_IMAGE = "home-assistant/builder/actions/build-image@"
HOSTED = {"ubuntu-24.04", "ubuntu-24.04-arm", "${{ matrix.os }}"}


class TestRunners:
    @pytest.mark.parametrize("name", ["build.yaml", "ci.yml", "frontend.yml"])
    def test_every_job_runs_on_a_github_hosted_runner(self, name):
        for job_name, job in _workflow(name)["jobs"].items():
            if "uses" in job:  # a called workflow (ci.yml, frontend.yml)
                assert job["uses"].startswith("./.github/workflows/"), job_name
                continue
            assert job["runs-on"] in HOSTED, job_name

    def test_called_gates_are_the_checked_workflows(self):
        jobs = _workflow("build.yaml")["jobs"]
        called = {job["uses"] for job in jobs.values() if "uses" in job}
        assert called == {
            "./.github/workflows/ci.yml",
            "./.github/workflows/frontend.yml",
        }

    def test_each_architecture_builds_on_its_native_runner(self):
        build = _workflow("build.yaml")["jobs"]["build"]
        assert build["runs-on"] == "${{ matrix.os }}"
        # The matrix comes from prepare-multi-arch-matrix: amd64 -> ubuntu-24.04,
        # aarch64 -> ubuntu-24.04-arm.
        assert (
            build["strategy"]["matrix"] == "${{ fromJSON(needs.init.outputs.matrix) }}"
        )

    def test_image_check_builds_both_architectures_natively_without_push(self):
        job = _workflow("ci.yml")["jobs"]["image"]
        assert job["runs-on"] == "${{ matrix.os }}"
        include = {(e["arch"], e["os"]) for e in job["strategy"]["matrix"]["include"]}
        assert include == {("amd64", "ubuntu-24.04"), ("aarch64", "ubuntu-24.04-arm")}
        (step,) = _uses(job, BUILD_IMAGE)
        assert step["with"]["push"] == "false"

    def test_image_check_runs_on_release_and_dependency_prs(self):
        cond = _workflow("ci.yml")["jobs"]["image"]["if"]
        assert "github.base_ref == 'main'" in cond
        assert "dependabot/docker/" in cond and "dependabot/pip/" in cond

    def test_every_release_builds_and_starts_the_image_before_pushing(self):
        """The release calls the backend gate with the image check, and every
        pushing job depends on that gate: nothing is published that has not
        been built and started once for both architectures."""
        cond = _workflow("ci.yml")["jobs"]["image"]["if"]
        assert cond.startswith("inputs.image ||")
        call = _workflow("ci.yml")[True]["workflow_call"]["inputs"]["image"]
        assert call["type"] == "boolean"
        jobs = _workflow("build.yaml")["jobs"]
        assert jobs["tests"]["with"]["image"] is True
        assert "tests" in jobs["init"]["needs"]
        assert jobs["build"]["needs"] == ["init", "bundle"]


class TestReferences:
    """Every ``steps.<id>`` and ``needs.<job>`` reference points at something that exists."""

    @pytest.mark.parametrize("name", ["build.yaml", "ci.yml", "frontend.yml"])
    def test_step_outputs_refer_to_steps_of_the_same_job(self, name):
        for job_name, job in _workflow(name)["jobs"].items():
            ids = {s["id"] for s in job.get("steps", []) if "id" in s}
            used = set(re.findall(r"steps\.([\w-]+)\.", json.dumps(job)))
            assert used <= ids, (job_name, used - ids)

    @pytest.mark.parametrize("name", ["build.yaml", "ci.yml", "frontend.yml"])
    def test_needs_outputs_refer_to_listed_jobs(self, name):
        jobs = _workflow(name)["jobs"]
        for job_name, job in jobs.items():
            needs = job.get("needs", [])
            needs = {needs} if isinstance(needs, str) else set(needs)
            assert needs <= set(jobs), job_name
            used = set(re.findall(r"needs\.([\w-]+)\.", json.dumps(job)))
            assert used <= needs, (job_name, used - needs)

    def test_bundle_download_waits_for_the_upload(self):
        jobs = _workflow("build.yaml")["jobs"]
        for job_name, job in jobs.items():
            if _uses(job, "actions/download-artifact@"):
                assert "bundle" in job["needs"], job_name


class TestTriggers:
    @staticmethod
    def _on(name: str) -> dict:
        wf = _workflow(name)
        return wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True

    def test_release_runs_on_pushes_to_main_and_on_version_tags(self):
        on = self._on("build.yaml")
        assert set(on) == {"push"}
        assert on["push"] == {"branches": ["main"], "tags": ["*.*.*"]}

    @pytest.mark.parametrize("name", ["ci.yml", "frontend.yml"])
    def test_gates_can_be_called_by_the_release(self, name):
        on = self._on(name)
        assert "workflow_call" in on and "pull_request" in on

    @pytest.mark.parametrize("name", ["ci.yml", "frontend.yml"])
    def test_gates_run_on_develop_and_on_main_only_through_the_release(self, name):
        # On main, build.yaml calls both gates; a push trigger would run them twice.
        assert self._on(name)["push"] == {"branches": ["develop"]}


# Lines in which two forms of a workflow may differ: comments, the runner, the
# Python and npm setup, the Playwright install and the trigger of build.yaml.
_VARIANT_LINE = re.compile(
    r"\s*(#.*"
    r"|runs-on: .*"
    r"|- uses: (actions/setup-python@\S+ # \S+|\./\.github/actions/[\w-]+)"
    r"|cache: (pip|npm)"
    r"|cache-dependency-path: .*"
    r"|(halumar|\.github)/\S+\.txt"
    r"|package-manager-cache: false"
    r"|- run: (uv )?pip install .*"
    r"|(uv )?pip install -r .*"
    r"|run: npx playwright install( --with-deps)? chromium"
    r"|push:|branches:|- main|tags:|- \"\*\.\*\.\*\"|workflow_dispatch:"
    r")?\s*"
)


@pytest.mark.skipif(
    not (os.path.isdir(_PUBLIC) and os.path.isdir(_OWN)),
    reason="only one form of the workflows in this checkout",
)
class TestTwoForms:
    """When the repository carries two forms of the workflows, they differ only
    in the lines of ``_VARIANT_LINE``; every other change is made in both."""

    @pytest.mark.parametrize("name", ["build.yaml", "ci.yml", "frontend.yml"])
    def test_forms_differ_only_in_known_lines(self, name):
        def common(*parts: str) -> list[str]:
            lines = _read(*parts).splitlines()
            return [x for x in lines if not _VARIANT_LINE.fullmatch(x)]

        a, b = common(_PUBLIC, name), common(_OWN, name)
        assert a == b, "\n".join(difflib.unified_diff(a, b, lineterm="", n=1))


class TestContractCheckout:
    def test_contract_comes_from_the_organisation_repository(self):
        action = yaml.safe_load(_read(ACTIONS, "contracts", "action.yml"))
        (checkout,) = [
            s
            for s in action["runs"]["steps"]
            if s.get("uses", "").startswith("actions/checkout@")
        ]
        repository = checkout["with"]["repository"]
        assert repository == "Halumar/contracts"  # brandlint: allow
        assert checkout["with"]["persist-credentials"] is False

    def test_test_job_gets_the_contract_directory(self):
        job = _workflow("ci.yml")["jobs"]["test"]
        assert any(
            s.get("id") == "contracts"
            and s.get("uses") == "./.github/actions/contracts"
            for s in job["steps"]
        )


class TestImageNames:
    @pytest.mark.parametrize("name", ["build.yaml", "ci.yml"])
    def test_no_owner_with_its_original_case_in_an_image_name(self, name):
        assert "ghcr.io/${{ github.repository_owner }}" not in _read(WORKFLOWS, name)

    def test_prefix_is_lowercased_once_and_used_everywhere(self):
        text = _read(WORKFLOWS, "build.yaml")
        assert "ghcr.io/${GITHUB_REPOSITORY_OWNER,,}" in text
        jobs = _workflow("build.yaml")["jobs"]
        (matrix,) = _uses(
            jobs["init"], "home-assistant/builder/actions/prepare-multi-arch-matrix@"
        )
        assert (
            matrix["with"]["registry-prefix"]
            == "${{ steps.prefix.outputs.registry-prefix }}"
        )
        (manifest,) = _uses(
            jobs["publish"],
            "home-assistant/builder/actions/publish-multi-arch-manifest@",
        )
        assert (
            manifest["with"]["registry-prefix"]
            == "${{ needs.init.outputs.registry-prefix }}"
        )

    def test_config_image_is_the_published_manifest(self):
        config = yaml.safe_load(_read(ADDON, "config.yaml"))
        assert config["image"] == "ghcr.io/halumar/hub"


class TestReleaseGate:
    def test_only_main_or_a_tag_publishes(self):
        script = _workflow("build.yaml")["jobs"]["release-gate"]["steps"][-1]["run"]
        assert 'GITHUB_REF}" != "refs/heads/main"' in script

    def test_tag_lookup_does_not_read_an_error_as_not_tagged(self):
        # Only an HTTP 404 from the API counts as "not tagged".
        script = _workflow("build.yaml")["jobs"]["release-gate"]["steps"][-1]["run"]
        assert "git ls-remote --exit-code" not in script
        assert "HTTP 404" in script


class TestBundle:
    def test_release_images_get_the_bundle_built_from_source(self):
        jobs = _workflow("build.yaml")["jobs"]
        bundle = jobs["bundle"]
        runs = " ".join(s.get("run", "") for s in bundle["steps"])
        assert "npm ci" in runs and "npm run build" in runs
        (upload,) = _uses(bundle, "actions/upload-artifact@")
        build = jobs["build"]
        assert "bundle" in build["needs"]
        steps = build["steps"]
        names = [s.get("uses", s.get("run", "")) for s in steps]
        download = next(
            i for i, n in enumerate(names) if n.startswith("actions/download-artifact@")
        )
        image = next(i for i, n in enumerate(names) if n.startswith(BUILD_IMAGE))
        assert download < image
        assert steps[download]["with"]["name"] == upload["with"]["name"]
        assert steps[download]["with"]["path"] == "halumar/rootfs/opt/hub/static"

    def test_image_check_builds_the_bundle_too(self):
        steps = _workflow("ci.yml")["jobs"]["image"]["steps"]
        runs = [s.get("run", "") for s in steps]
        assert any("npm run build" in r for r in runs)

    def test_frontend_gate_audits_bundled_packages(self):
        runs = " ".join(
            s.get("run", "")
            for s in _workflow("frontend.yml")["jobs"]["build-and-test"]["steps"]
        )
        assert "npm run licenses" in runs  # writes bundle-packages.json
        assert "npm-audit-bundle.mjs" in runs


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestAuditScript:
    AUDIT = {
        "vulnerabilities": {
            "@sveltejs/kit": {
                "via": [
                    {
                        "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                        "severity": "high",
                        "range": "<3",
                        "title": "t",
                    }
                ],
            },
            "vite": {
                "via": [
                    {
                        "url": "https://github.com/advisories/GHSA-dddd-eeee-ffff",
                        "severity": "high",
                        "range": "<7",
                        "title": "t",
                    }
                ],
            },
            "@sveltejs/adapter-static": {"via": ["@sveltejs/kit"]},
        }
    }

    def _run(self, tmp_path, allow: str) -> subprocess.CompletedProcess:
        (tmp_path / "audit.json").write_text(json.dumps(self.AUDIT))
        (tmp_path / "bundle.json").write_text(json.dumps(["@sveltejs/kit", "svelte"]))
        (tmp_path / "allow.txt").write_text(allow)
        return subprocess.run(
            ["node", AUDIT_SCRIPT, "audit.json", "bundle.json", "allow.txt"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

    def test_bundled_dev_dependency_with_an_advisory_fails(self, tmp_path):
        # `npm audit --omit=dev` reports nothing here: kit is a devDependency.
        result = self._run(tmp_path, "")
        assert result.returncode == 1
        assert "GHSA-aaaa-bbbb-cccc" in result.stdout
        assert "GHSA-dddd-eeee-ffff" not in result.stdout  # vite is not in the bundle

    def test_allowed_advisory_passes(self, tmp_path):
        result = self._run(tmp_path, "GHSA-aaaa-bbbb-cccc  # server only\n")
        assert result.returncode == 0, result.stdout

    def test_missing_report_fails(self, tmp_path):
        (tmp_path / "audit.json").write_text(
            json.dumps({"error": {"code": "ENOTFOUND"}})
        )
        (tmp_path / "bundle.json").write_text("[]")
        (tmp_path / "allow.txt").write_text("")
        result = subprocess.run(
            ["node", AUDIT_SCRIPT, "audit.json", "bundle.json", "allow.txt"],
            cwd=tmp_path,
            capture_output=True,
        )
        assert result.returncode == 2
