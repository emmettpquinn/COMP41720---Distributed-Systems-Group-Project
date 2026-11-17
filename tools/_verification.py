"""Helpers for checks that own a fresh disposable Compose project."""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def external_output(path):
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Keep generated verification output outside the repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def wait_for(probe, timeout=90):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except (AssertionError, OSError, TimeoutError, subprocess.CalledProcessError) as error:
            last_error = type(error).__name__
        time.sleep(0.25)
    raise AssertionError(f"Probe timed out after {timeout}s ({last_error})")


def http(origin, path, body=None, expected=200, basic=None, method=None):
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    request = urllib.request.Request(
        origin + path, data=json.dumps(body).encode() if body is not None else None,
        headers=headers, method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read().decode()
        if response.code != expected:
            raise AssertionError(f"{path}: expected {expected}, received {response.code}: {content[:200]}")
        return json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content


def source_info():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).split(b"\0")
    digest = hashlib.sha256()
    for filename in sorted(set(paths)):
        if not filename:
            continue
        path = ROOT / filename.decode()
        if path.is_file():
            digest.update(filename + b"\0" + path.read_bytes())
    return {"head": revision, "source_sha256": digest.hexdigest(),
            "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))}


class Project:
    """Never attach to an existing project; cleanup deletes only generated data."""

    def __init__(self, label, output, source=ROOT, full=False, messaging=False, extra=""):
        self.name = "catalogue-" + label + "-" + uuid.uuid4().hex[:10]
        self.source = Path(source)
        self.output = external_output(output)
        self.log = self.output.with_suffix(".log")
        self.full = full
        self.messaging = messaging
        self.extra = extra

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix=self.name + "-")
        override = Path(self.temp.name) / "ports.yml"
        ports = {"catalogue": [8000], "rabbitmq": [15672]}
        if self.full:
            ports.update({name: [8000] for name in ("booking", "discovery", "payment", "ticketing", "gateway")})
            ports.update({"prometheus": [9090], "grafana": [3000], "mailhog": [8025]})
        lines = ["services:"]
        for service, values in ports.items():
            mappings = ", ".join(f'"127.0.0.1::{value}"' for value in values)
            lines += [f"  {service}:", f"    ports: !override [{mappings}]"]
        override.write_text("\n".join(lines) + "\n" + self.extra)
        self.command = ["docker", "compose", "-p", self.name, "-f", str(self.source / "docker-compose.yml")]
        if self.full:
            for filename in ("docker-compose.dev.yml", "docker-compose.monitoring.yml"):
                self.command += ["-f", str(self.source / filename)]
        self.command += ["-f", str(override)]
        self.env = os.environ | {
            "CATALOGUE_MESSAGING_ENABLED": str(self.messaging).lower(),
            "BOOKING_DEMO_SEED": "true", "PAYMENT_MODE": "ok", "PAYMENT_DELAY": "0",
            "PAYMENT_ERROR_RATE": "0", "GRAFANA_PASSWORD": "stagedoor-demo",
        }
        existing = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.name], text=True,
        ).strip()
        if existing:
            raise RuntimeError("Refusing to reuse an existing project")
        self.log.write_text("")
        return self

    def cli(self, *arguments, timeout=300):
        result = subprocess.run(self.command + list(arguments), cwd=self.source, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        with self.log.open("a") as log:
            log.write("compose " + " ".join(arguments) + "\n" + result.stdout + "\n")
        if result.returncode:
            raise RuntimeError(f"Compose {arguments[0]} failed; inspect {self.log}")
        return result.stdout.strip()

    def origin(self, service, port=8000):
        # Docker can reassign ephemeral host ports when a container restarts.
        return "http://" + self.cli("port", service, str(port))

    def sql(self, service, query):
        owner = service.removesuffix("-db")
        return self.cli("exec", "-T", service, "psql", "-U", owner, "-d", owner,
                        "-v", "ON_ERROR_STOP=1", "-Atc", query)

    def __exit__(self, *exception):
        try:
            self.cli("down", "--volumes", "--remove-orphans")
        finally:
            self.temp.cleanup()
