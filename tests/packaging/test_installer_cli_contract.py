"""Contract tests for ``install.sh`` and the packaged ``soctalk`` CLI.

No cluster and no network: the behavioural tests source ``install.sh`` (which is
sourceable by design, ``infra/packer/scripts/firstboot.sh`` does it) or run the
CLI with ``kubectl`` / ``firewall-cmd`` stubbed onto an isolated ``PATH``. They
guard three classes of regression that only surface on a real host and are
therefore expensive to catch any other way.

1. **RHEL ``sudo secure_path``.** k3s and Helm install their binaries (and the
   ``kubectl`` symlink) into ``/usr/local/bin``. That directory is not on the
   sudo ``secure_path`` of RHEL-family distros
   (``Defaults secure_path = /sbin:/bin:/usr/sbin:/usr/bin``), so any
   ``sudo k3s …`` / ``sudo kubectl …`` / ``sudo helm …`` we *execute* or *print
   for the operator to run* is "command not found" on Rocky/RHEL/Fedora/Alma.
   Commit f78c7f8 fixed the executed calls; issue #116 was the same bug in the
   post-install summary the installer prints.

2. **Silent zero-match selectors.** ``kubectl logs -l`` with zero matches is not
   an error, so a wrong label selector fails with exit 0 and no output. That is
   how #117 went unnoticed: ``soctalk logs`` never matched a pod, for any
   component, on any distro.

3. **Late, opaque environment failures.** firewalld drops flannel traffic, so on
   RHEL the install passed preflight and then died fifteen minutes later on a
   helm ``--wait`` timeout (#118). Preflight has to catch it before the host is
   mutated.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"
CLI = ROOT / "packaging" / "soctalk"
CHART_TEMPLATES = ROOT / "charts" / "soctalk-system" / "templates"

# Binaries k3s/helm drop into /usr/local/bin. A bare `sudo <bin>` cannot
# resolve on a RHEL-family host.
LOCAL_BIN_TOOLS = ("k3s", "kubectl", "helm")


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file is missing: {path}"
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop whole-line ``#`` comments so prose *about* a bug isn't mistaken for
    the bug. Only used by the ``sudo``-prefix scan, whose patterns cannot occur
    in a trailing comment on a line that also contains real code."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


# --------------------------------------------------------------------------- #
# sudo secure_path (#116)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", LOCAL_BIN_TOOLS)
def test_installer_never_emits_bare_sudo_local_bin_tool(tool):
    """``install.sh`` must not print or run ``sudo <tool>`` unqualified.

    Regression #116: ``print_summary`` told the operator to run
    ``sudo k3s kubectl -n soctalk-system get pods``, which on Rocky Linux 9
    answers ``sudo: k3s: command not found`` immediately after a *successful*
    install. An absolute path (or a ``command -v``-resolved one) is required;
    the ``sudo /usr/local/bin/k3s-uninstall.sh`` line in the same block was
    already doing it correctly.
    """
    body = _strip_comments(_read(INSTALL_SH))
    bad = re.findall(rf"sudo\s+{tool}\b", body)
    assert not bad, (
        f"install.sh emits bare `sudo {tool}` {len(bad)}x — unresolvable under "
        f"RHEL's sudo secure_path. Use an absolute path or a command -v-resolved "
        f"variable (see #116)."
    )


@pytest.mark.parametrize("tool", LOCAL_BIN_TOOLS)
def test_cli_never_emits_bare_sudo_local_bin_tool(tool):
    """Same guard for the packaged CLI's own output and calls."""
    body = _strip_comments(_read(CLI))
    bad = re.findall(rf"sudo\s+{tool}\b", body)
    assert not bad, f"packaging/soctalk emits bare `sudo {tool}` — see #116."


# --------------------------------------------------------------------------- #
# print_summary (#116) — behavioural
# --------------------------------------------------------------------------- #


def _run_print_summary(tmp_path, admin_email: str):
    """Source install.sh, stub out the cluster lookup, run print_summary.

    ``resolve_admin_email`` is redefined *after* sourcing so this exercises
    print_summary's own branching without needing a cluster.
    """
    script = (
        f'. "{INSTALL_SH}"\n'
        # localhost skips the `hostname -I` hint branch, keeping this hermetic.
        "HOSTNAME_IN=localhost\n"
        f'resolve_admin_email() {{ printf %s "{admin_email}"; }}\n'
        "print_summary\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    return proc.returncode, proc.stdout


def test_summary_prints_login_when_the_admin_is_known(tmp_path):
    rc, out = _run_print_summary(tmp_path, "admin@example.test")
    assert rc == 0, f"print_summary exited {rc}: {out!r}"
    assert "Login:     admin@example.test" in out


def test_summary_omits_the_login_line_when_the_admin_is_unknown(tmp_path):
    """Regression #116: the ``--values-file`` path never sets ``$ADMIN_EMAIL``,
    so the summary printed the label ``Login:`` with nothing after it. Better to
    drop the line than to show a dangling label."""
    rc, out = _run_print_summary(tmp_path, "")
    assert rc == 0, f"print_summary exited {rc}: {out!r}"
    assert "Login:" not in out, f"dangling Login label in summary: {out!r}"
    # The rest of the block must survive the missing value.
    assert "SocTalk is installed." in out
    assert "URL:" in out and "Uninstall:" in out


def test_summary_pods_command_is_runnable_under_sudo(tmp_path):
    """The ``Pods:`` line has to name an absolute binary, like the
    ``Uninstall:`` line two rows below it already did."""
    _, out = _run_print_summary(tmp_path, "admin@example.test")
    pods = [ln for ln in out.splitlines() if "Pods:" in ln]
    assert pods, f"no Pods: line in summary: {out!r}"
    assert re.search(r"sudo\s+/\S+/k3s\s+kubectl", pods[0]), (
        f"Pods: line is not an absolute path, so it 404s under RHEL sudo: {pods[0]!r}"
    )


def test_resolve_admin_email_searches_init_containers_too():
    """The lookup must not pin ``containers[0]``.

    ``SOCTALK_BOOTSTRAP_ADMIN_EMAIL`` is wired onto the api Deployment's
    ``db-init`` **init** container (that is what seeds the first admin), not the
    app container. A ``containers[0]``-indexed jsonpath matches nothing and
    silently drops the Login line again — caught only on a live cluster.
    Recursive descent (``spec..env``) finds it in either position.
    """
    body = _read(INSTALL_SH)
    fn = re.search(r"resolve_admin_email\(\)\s*\{(.*?)\n\}", body, re.DOTALL)
    assert fn, "resolve_admin_email() not found in install.sh"
    block = fn.group(1)
    assert "spec..env[?(@.name==" in block, (
        "resolve_admin_email no longer uses jsonpath recursive descent; the "
        "bootstrap-admin env var lives on an init container (#116)."
    )
    assert not re.search(r"spec\.containers\[0\]\.env", block), (
        "resolve_admin_email pins containers[0], which never holds "
        "SOCTALK_BOOTSTRAP_ADMIN_EMAIL (#116)."
    )
    assert ".data.password" not in block, (
        "resolve_admin_email reads the bootstrap password — it only needs the email."
    )


# --------------------------------------------------------------------------- #
# soctalk logs (#117) — behavioural, with kubectl stubbed
# --------------------------------------------------------------------------- #


def _run_cli_logs(tmp_path, comp: str, *, pods: str = "", rc_get: int = 0):
    """Run ``packaging/soctalk logs <comp>`` against a stubbed ``kubectl``.

    ``pods`` is what ``get pods -o name`` prints; ``rc_get`` its exit status
    (non-zero simulates an unreachable cluster). Every invocation is appended to
    a log so the test can assert whether ``kubectl logs`` was ever reached.
    """
    binhome = tmp_path / "bin"
    binhome.mkdir()
    calls = tmp_path / "calls.txt"
    _stub(
        binhome / "kubectl",
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "logs" ]; then echo "STUB_LOG_LINE"; exit 0; fi\n'
        "done\n"
        'case "$*" in\n'
        f'  *"get pods"*"-o name"*) printf "%s" "{pods}"; exit {rc_get} ;;\n'
        '  *"get pods"*) echo "api app-ui postgres" ;;\n'
        "esac\nexit 0\n",
    )
    proc = subprocess.run(
        ["bash", str(CLI), "logs", comp],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{binhome}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SOCTALK_CLI_VERSION": "test",
        },
    )
    invoked = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return proc, invoked


def test_cli_logs_streams_when_the_component_exists(tmp_path):
    proc, invoked = _run_cli_logs(tmp_path, "api", pods="pod/soctalk-system-api-abc")
    assert proc.returncode == 0, proc.stderr
    assert "STUB_LOG_LINE" in proc.stdout
    assert " logs " in f" {invoked} ", f"kubectl logs was never called: {invoked!r}"
    assert "app.kubernetes.io/component=api" in invoked, (
        f"logs did not select on the component label (#117): {invoked!r}"
    )


def test_cli_logs_fails_loudly_on_an_unknown_component(tmp_path):
    """Regression #117: zero matches exited 0 with no explanation.

    This is the whole reason the bug survived: ``kubectl logs -l`` treats a
    selector that matches nothing as success.
    """
    proc, invoked = _run_cli_logs(tmp_path, "nosuchthing", pods="")
    assert proc.returncode != 0, (
        f"unknown component exited 0 — the #117 failure mode: {proc.stdout!r}"
    )
    assert "nosuchthing" in proc.stderr
    assert " logs " not in f" {invoked} ", (
        f"streamed logs for a component with no pods: {invoked!r}"
    )


def test_cli_logs_distinguishes_an_unreachable_cluster_from_a_missing_component(
    tmp_path,
):
    """A failed query must not be reported as "no such component", or the
    operator goes hunting the wrong problem."""
    proc, _ = _run_cli_logs(tmp_path, "api", pods="", rc_get=1)
    assert proc.returncode != 0
    assert "cluster" in proc.stderr.lower(), (
        f"an unreachable cluster was reported as a missing component: {proc.stderr!r}"
    )


def _chart_component_labels() -> set[str]:
    """Every ``app.kubernetes.io/component: <x>`` literal the chart emits."""
    found: set[str] = set()
    for tpl in CHART_TEMPLATES.glob("*.yaml"):
        for m in re.finditer(
            r"app\.kubernetes\.io/component:\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
            tpl.read_text(encoding="utf-8"),
        ):
            found.add(m.group(1))
    return found


def test_cli_logs_selector_matches_a_label_the_chart_actually_sets():
    """Drift guard between the chart and the CLI.

    The behavioural tests above stub kubectl, so they cannot notice the chart
    renaming its labels out from under the CLI. This can.
    """
    components = _chart_component_labels()
    assert {"api", "app-ui"} <= components, (
        f"soctalk-system chart no longer labels workloads by component "
        f"(found {sorted(components)}); the CLI's logs selector is built on it."
    )
    body = _read(CLI)
    logs_block = re.search(r"^\s*logs\)(.*?)^\s*;;", body, re.MULTILINE | re.DOTALL)
    assert logs_block, "could not locate the `logs)` case arm in packaging/soctalk"
    assert "app.kubernetes.io/component=" in logs_block.group(1), (
        "soctalk logs no longer selects on app.kubernetes.io/component — the only "
        "label that distinguishes soctalk-system workloads (#117)."
    )


# --------------------------------------------------------------------------- #
# check_firewalld (#118) — behavioural, with firewall-cmd stubbed
# --------------------------------------------------------------------------- #


def _run_check_firewalld(tmp_path, stub: str | None):
    """Source install.sh and run check_firewalld with ``firewall-cmd`` stubbed.

    ``stub`` is the body of a fake ``firewall-cmd``; ``None`` means the binary is
    absent. PATH is narrowed *after* sourcing, because install.sh prepends
    /usr/local/bin to PATH on the way in and a real firewall-cmd anywhere on the
    inherited PATH would make the "absent" case pass for the wrong reason.
    check_firewalld itself shells out to nothing but firewall-cmd.
    """
    binhome = tmp_path / "bin"
    binhome.mkdir()
    if stub is not None:
        _stub(binhome / "firewall-cmd", stub)
    script = f'. "{INSTALL_SH}"\nPATH="{binhome}"\ncheck_firewalld\n'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    return proc.returncode, proc.stdout


# Answers `--query-source` / `--query-interface` from a whitespace list of
# things the trusted zone holds, exactly as firewalld does (exit 0 = yes).
_QUERY_STUB = """#!/bin/sh
case "$1" in
  --state) exit 0 ;;
esac
want=""
for a in "$@"; do
  case "$a" in
    --query-source=*) want="${a#--query-source=}" ;;
    --query-interface=*) want="${a#--query-interface=}" ;;
  esac
done
for have in %s; do
  [ "$have" = "$want" ] && exit 0
done
exit 1
"""


def test_check_firewalld_flags_a_running_untrusted_firewall(tmp_path):
    """The Rocky 9 default posture must be reported as a problem (#118).

    firewalld running, ``trusted`` zone empty: this is the configuration that
    silently breaks pod-to-pod traffic and kills the install on a helm timeout.
    """
    rc, out = _run_check_firewalld(tmp_path, _QUERY_STUB % "")
    assert rc != 0, "an active firewalld with untrusted k3s networks must fail preflight"
    assert "firewalld" in out and "running" in out
    # The operator has to be handed the exact remedy, not just a diagnosis.
    assert "--zone=trusted --add-source=10.42.0.0/16" in out
    assert "--zone=trusted --add-source=10.43.0.0/16" in out
    assert "firewall-cmd --reload" in out


def test_check_firewalld_accepts_trusted_k3s_cidrs(tmp_path):
    """The documented remedy must read as ok, or the check cries wolf forever."""
    rc, out = _run_check_firewalld(
        tmp_path, _QUERY_STUB % "10.42.0.0/16 10.43.0.0/16"
    )
    assert rc == 0, "the documented firewalld remedy must satisfy the check"
    assert "ok" in out and "trusted" in out


def test_check_firewalld_accepts_trusted_cni_interfaces(tmp_path):
    """Trusting the flannel interfaces is an equally valid configuration."""
    rc, out = _run_check_firewalld(tmp_path, _QUERY_STUB % "cni0 flannel.1")
    assert rc == 0
    assert "ok" in out


def test_check_firewalld_requires_both_pod_and_service_cidrs(tmp_path):
    """Pod CIDR alone is not enough: ClusterIP traffic uses the service CIDR."""
    rc, _ = _run_check_firewalld(tmp_path, _QUERY_STUB % "10.42.0.0/16")
    assert rc != 0, "only the pod CIDR trusted must still be reported"


def test_check_firewalld_rejects_a_lookalike_cidr(tmp_path):
    """Detection must be exact, not a substring match.

    ``10.42.1.0/24`` and ``10.43.1.0/24`` both *contain* the prefixes a naive
    check looks for, while trusting neither of the k3s defaults. Waving that
    host through is exactly the failure this check exists to prevent.
    """
    rc, _ = _run_check_firewalld(tmp_path, _QUERY_STUB % "10.42.1.0/24 10.43.1.0/24")
    assert rc != 0, "a lookalike CIDR was accepted as the k3s default"


def test_check_firewalld_silent_when_firewalld_is_installed_but_stopped(tmp_path):
    """``--state`` non-zero means not running: nothing to warn about."""
    rc, out = _run_check_firewalld(tmp_path, "#!/bin/sh\nexit 1\n")
    assert rc == 0
    assert out.strip() == "", f"unexpected output for a stopped firewalld: {out!r}"


def test_check_firewalld_silent_when_absent(tmp_path):
    """Debian/Ubuntu and the RHEL cloud images have no firewall-cmd at all."""
    rc, out = _run_check_firewalld(tmp_path, None)
    assert rc == 0
    assert out.strip() == ""


def test_check_firewalld_needs_no_external_commands(tmp_path):
    """It must still print with nothing but the stub on PATH.

    ``_run_check_firewalld`` narrows PATH to the stub directory alone, so a
    heredoc (``cat``) or any other external in the warning path would break
    this. Keeping the branch builtin-only is also why it survives a host whose
    PATH is part of the problem.
    """
    rc, out = _run_check_firewalld(tmp_path, _QUERY_STUB % "")
    assert rc != 0
    assert "sudo firewall-cmd --reload" in out


def test_preflight_consults_check_firewalld():
    """Wire-up guard: the function must actually be reached from preflight, and
    its non-zero return has to count as a preflight failure rather than abort
    the installer under ``set -e``."""
    body = _strip_comments(_read(INSTALL_SH))
    assert re.search(r"check_firewalld\s*\|\|\s*fail=1", body), (
        "preflight no longer runs check_firewalld (or no longer counts it as a "
        "preflight failure) — see #118."
    )
