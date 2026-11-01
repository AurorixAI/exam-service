#!/usr/bin/env python3
"""Reuse the image that stage already built for this revision (CI speed pack #140, B). Runs ON THE SERVER during the deploy, from the checkout of the
revision being deployed. Stdlib only; the docker CLI is the only thing it calls.

    docker compose -f COMPOSE ... config --format json | release_image.py fingerprint --service S
        sha256 of everything that decides what the build produces except the source (the ``build`` section of the service without ``context`` and
        ``labels``: dockerfile, target, args ...). The env-file of each stack is already resolved inside, so a different ``NEXT_PUBLIC_*`` of
        stage and production gives a different fingerprint.
    release_image.py find --sha SHA --fingerprint FP --archive ARCHIVE_SHA256 --service S
        prints the id of an image on this server built for exactly this revision, source archive, build configuration and service; nothing when
        there is none (or any doubt). The caller builds then, as before.
    release_image.py note --file release-record.json KEY=VALUE ...
        adds keys to the release record.

An image is reused only if ALL four labels agree (set by the deploy when it builds): org.opencontainers.image.revision, io.algolab.source.archive.sha256,
io.algolab.build.fingerprint, io.algolab.service. Fail closed: no label, a second opinion, a docker error = build.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

REVISION = "org.opencontainers.image.revision"
ARCHIVE = "io.algolab.source.archive.sha256"
FINGERPRINT = "io.algolab.build.fingerprint"
SERVICE = "io.algolab.service"
DOCKER = os.environ.get("RELEASE_IMAGE_DOCKER", "docker")


def fingerprint(config, service):
    """Canonical hash of the build configuration of ``service`` (``docker compose config --format json`` as a dict)."""
    build = config["services"][service]["build"]
    if isinstance(build, str):                      # the short form «build: ./dir» has no other settings
        build = {"context": build}
    rest = {k: v for k, v in build.items() if k not in {"context", "labels"}}
    canonical = json.dumps({"service": service, "build": rest}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pick(images, sha, fp, archive, service):
    """-> (image_id or None, reason). ``images`` is the output of ``docker image inspect`` (a list). The newest image whose four labels all match."""
    if not (sha and fp and archive and service):
        return None, "an identity part is empty"
    want = {REVISION: sha, FINGERPRINT: fp, ARCHIVE: archive, SERVICE: service}
    matching, near = [], []
    for image in images:
        labels = ((image.get("Config") or {}).get("Labels")) or {}
        hits = [k for k, v in want.items() if labels.get(k) == v]
        if len(hits) == len(want):
            matching.append(image)
        elif REVISION in hits:
            near.append(sorted(set(want) - set(hits)))
    if matching:
        newest = max(matching, key=lambda i: i.get("Created", ""))
        return newest["Id"], f"{len(matching)} image(s) match the revision, the source archive, the build configuration and the service"
    if near:
        return None, "images of this revision exist but differ in: " + ", ".join(sorted({x for group in near for x in group}))
    return None, "no image of this revision on the server"


def docker(*args):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=120, check=True).stdout


def find(sha, fp, archive, service):
    try:
        ids = docker("image", "ls", "--no-trunc", "-q", "--filter", f"label={REVISION}={sha}").split()
        images = json.loads(docker("image", "inspect", *sorted(set(ids)))) if ids else []
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return None, f"docker did not answer clearly ({type(error).__name__})"
    return pick(images, sha, fp, archive, service)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fingerprint")
    f.add_argument("--service", required=True)
    s = sub.add_parser("find")
    for name in ("sha", "fingerprint", "archive", "service"):
        s.add_argument("--" + name, required=True)
    n = sub.add_parser("note")
    n.add_argument("--file", required=True)
    n.add_argument("pairs", nargs="+")
    args = parser.parse_args(argv)

    if args.cmd == "fingerprint":
        print(fingerprint(json.load(sys.stdin), args.service))
        return 0
    if args.cmd == "find":
        image_id, reason = find(args.sha, args.fingerprint, args.archive, args.service)
        print(reason, file=sys.stderr)
        if image_id:
            print(image_id)
        return 0
    with open(args.file, encoding="utf-8") as handle:
        record = json.load(handle)
    record.update(pair.split("=", 1) for pair in args.pairs)
    with open(args.file, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
