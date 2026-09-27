"""Inventory actual installed packages and bundled notices, without registry requests."""
import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET
import zipfile
import importlib.metadata as metadata
from pathlib import Path
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "licenses"
OUT.mkdir(exist_ok=True)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--python-only", action="store_true")
parser.add_argument("--python-image", help="Use a built local backend image without restarting services")
options = parser.parse_args()


def command(*args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True).stdout


def inside(service, *args):
    if service == "backend" and options.python_image:
        return command(
            "docker", "run", "--rm", "--pull", "never", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            options.python_image, *args,
        )
    return command("docker", "compose", "exec", "-T", service, *args)


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def table(name, title, rows):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    body = [f"# {title}", "", "| Компонент | Версия | Лицензия из поставки |", "| --- | --- | --- |"]
    body += ["| " + " | ".join(cell(row[key]) for key in ("name", "version", "license")) + " |" for row in rows]
    (OUT / name).write_text("\n".join(body) + "\n", encoding="utf-8")


def notice_license(notices):
    text = "\n".join(notices.values())
    spdx = re.search(r"SPDX-License-Identifier: (.+)", text)
    if spdx:
        return spdx[1].strip()
    if "UNICODE LICENSE V3" in text:
        return "Unicode-3.0"
    if "Apache License" in text and "Version 2.0" in text:
        return "Apache-2.0"
    if "Redistribution and use in source and binary forms" in text:
        return "BSD-style; exact conditions in embedded notice"
    return ""


sources = json.loads((ROOT / "scripts/java-license-sources.json").read_text(encoding="utf-8"))


METADATA = r'''
import importlib.metadata as m, json
rows=[]
for d in m.distributions():
    if d.metadata['Name'] == 'dispatcher112': continue
    license = d.metadata.get('License-Expression') or d.metadata.get('License')
    if not license or len(license)>150:
        license = ' / '.join(c.split(' :: ')[-1] for c in d.metadata.get_all('Classifier', []) if c.startswith('License ::'))
    notices={}
    for f in d.files or []:
        if any(word in f.name.lower() for word in ('license','copying','notice')):
            path=d.locate_file(f)
            if path.is_file() and path.stat().st_size < 300000:
                notices[str(f)]=path.read_text(errors='replace')
    rows.append(dict(name=d.metadata['Name'],version=d.version,license=license or 'See bundled notices',notices=notices))
print(json.dumps(sorted(rows,key=lambda r:r['name'].lower())))
'''
python = json.loads(inside("backend", "python", "-c", METADATA))
save("python.json", python)
table("PYTHON.md", "Python: runtime, разработка и проверки", python)
if options.python_only:
    print(json.dumps({"python_packages": len(python), "source": options.python_image or "running backend"}))
    raise SystemExit(0)
lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
npm = [dict(name=path.removeprefix("node_modules/"), version=row["version"], license=row["license"],
            development=row.get("dev", False), optional=row.get("optional", False), integrity=row.get("integrity"))
       for path, row in lock["packages"].items() if path]
save("npm.json", npm)
table("NPM.md", "npm: все записи package-lock.json, включая optional/dev", npm)

# Read the publisher's distribution, including jars' own POMs and notices.
packed = inside("languagetool", "tar", "-czf", "-", "libs", "third-party-licenses", "COPYING.txt", "README.md", "languagetool-server.jar")
java = []
with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
    publisher = archive.extractfile("third-party-licenses/README.txt").read().decode()
    (OUT / "LANGUAGETOOL-PUBLISHER.txt").write_text(publisher, encoding="utf-8")
    notices = {}
    for member in archive.getmembers():
        if not member.isfile():
            continue
        data = archive.extractfile(member).read()
        if not member.name.endswith(".jar"):
            notices[member.name] = data.decode(errors="replace")
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as jar:
            props, licenses, embedded, manifest = {}, [], {}, {}
            for path in jar.namelist():
                lower = path.lower()
                if lower.endswith("pom.properties"):
                    candidate = {key: value.strip() for key, value in re.findall(r"^(artifactId|version|groupId)=(.+)$", jar.read(path).decode(), re.M)}
                    if not props or candidate.get("artifactId") == Path(member.name).stem:
                        props = candidate
                if lower == "meta-inf/manifest.mf":
                    text = jar.read(path).decode().replace("\r\n ", "").replace("\n ", "")
                    manifest = dict(re.findall(r"^([^:\r\n]+): (.+)\r?$", text, re.M))
                if lower.endswith("pom.xml"):
                    pom = ET.fromstring(jar.read(path))
                    for node in pom.findall("{*}licenses/{*}license/{*}name"):
                        if node.text: licenses.append(node.text)
                if any(part in lower for part in ("license", "copying", "notice", "readme")) and not lower.endswith("/"):
                    if jar.getinfo(path).file_size < 300000:
                        embedded[path] = jar.read(path).decode(errors="replace")
            artifact = props.get("artifactId", Path(member.name).stem)
            fallback = [line.split(" - ", 1)[-1] for line in publisher.splitlines() if f":{artifact}:" in line]
            source = sources.get(artifact, {})
            row = dict(name=artifact, jar=member.name,
                       version=props.get("version") or manifest.get("Implementation-Version") or manifest.get("Bundle-Version") or "distribution 6.6; see SHA-256",
                       sha256=hashlib.sha256(data).hexdigest(),
                       license=" / ".join(dict.fromkeys(licenses)) or notice_license(embedded) or " / ".join(fallback) or source.get("license", "See embedded notices"),
                       notices=embedded, source=source.get("source", "embedded POM/notices or publisher's distribution"))
            java.append(row)
    save("languagetool-notices.json", notices)
save("java.json", sorted(java, key=lambda row: row["name"]))
table("JAVA.md", "LanguageTool 6.6: фактические JAR, POM и notices", sorted(java, key=lambda row: row["name"]))

systems = {}
for service in ("backend", "frontend", "postgres", "redis", "languagetool", "backup", "gateway", "telephony", "ollama"):
    if service in {"gateway", "telephony"}:
        installed = inside(service, "cat", "/lib/apk/db/installed").decode()
        rows = []
        for block in installed.strip().split("\n\n"):
            fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            rows.append(dict(name=fields["P"], version=fields["V"], license=fields.get("L", "See package notice")))
        systems[service] = rows
    else:
        installed = inside(service, "dpkg-query", "-W", "-f=${Package}\t${Version}\n").decode()
        packed = inside(service, "sh", "-c", "tar -chzf - /usr/share/doc/*/copyright /usr/share/common-licenses 2>/dev/null")
        (OUT / f"{service}-os-notices.tar.gz").write_bytes(packed)
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive:
            notices = {member.name: archive.extractfile(member).read().decode(errors="replace")
                       for member in archive.getmembers() if member.isfile()}
        rows = []
        for line in installed.splitlines():
            name, version = line.split("\t")
            notice = notices.get(f"usr/share/doc/{name}/copyright", "")
            licenses = list(dict.fromkeys(re.findall(r"^License: (.+)$", notice, re.M)))
            rows.append(dict(name=name, version=version, license=" / ".join(licenses) or "See package copyright in bundled OS notices"))
        systems[service] = rows
    table(f"SYSTEM-{service}.md", f"Системные пакеты образа {service}", systems[service])
save("system.json", systems)

pending = ["torch", "playwright", "httpx", "websockets"]
tool_rows = {}
while pending:
    name = pending.pop()
    dist = metadata.distribution(name)
    canonical = dist.metadata["Name"]
    if canonical.lower() in tool_rows:
        continue
    license = dist.metadata.get("License-Expression") or dist.metadata.get("License", "")
    if not license or len(license) > 150:
        license = " / ".join(c.split(" :: ")[-1] for c in dist.metadata.get_all("Classifier", []) if c.startswith("License ::"))
    tool_rows[canonical.lower()] = dict(name=canonical, version=dist.version, license=license or "See installed distribution notices")
    for dependency in dist.requires or []:
        requirement = Requirement(dependency)
        if not requirement.marker or requirement.marker.evaluate({"extra": ""}):
            pending.append(requirement.name)
table("TOOLS.md", "Инструменты приёмки и сборки WAV: установленное окружение Windows", sorted(tool_rows.values(), key=lambda row: row["name"]))
save("tools.json", list(tool_rows.values()))
print(json.dumps({"python": len(python), "npm": len(npm), "java": len(java), "system": {key: len(rows) for key, rows in systems.items()},
                  "java_without_license_label": [row["name"] for row in java if row["license"] == "See embedded notices"]}))
