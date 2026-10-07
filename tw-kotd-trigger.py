#!/usr/bin/env python3
"""Poll Kernel:HEAD per-arch repos and trigger openQA KOTD jobs on new builds.

Requires: pip install openqa_client requests
Requires: /etc/openqa/client.conf or ~/.config/openqa/client.conf with an
          [openqa.opensuse.org] key/secret

Usage:
    ./kotd_trigger.py             # dry run (default) - prints what it would do
    ./kotd_trigger.py --commit    # actually posts to openQA
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from openqa_client.client import OpenQA_Client

log = logging.getLogger("kotd-trigger")

OPENQA_HOST = "openqa.opensuse.org"
GROUP_ID = 140
STATE_FILE = Path(__file__).parent / "tw_kotd_trigger_state.json"
REPOMD_NS = {"r": "http://linux.duke.edu/metadata/repo"}
PRIMARY_NS = {"c": "http://linux.duke.edu/metadata/common"}

BASE_HDD_KEY_MAP = {
    "HDD_1": "BASE_HDD_1",
}
BASE_HDD_KEY_MAP_UEFI = {
    "HDD_1": "BASE_HDD_1",
    "UEFI_PFLASH_VARS": "BASE_UEFI_PFLASH_VARS",
}
XFSTESTS_HDD_KEY_MAP = {
    "PUBLISH_HDD_1": "XFSTESTS_BASE_HDD_1",
}
XFSTESTS_HDD_KEY_MAP_UEFI = {
    "PUBLISH_HDD_1": "XFSTESTS_BASE_HDD_1",
    "PUBLISH_PFLASH_VARS": "XFSTESTS_BASE_UEFI_PFLASH_VARS",
}

# o3_group_id below: base images to install kotd on top, are picked from the
# latest jobs in job group 32 (the kernel group).
ARCHES = {
    "x86_64": {
        "kotd_repo": "https://download.opensuse.org/repositories/Kernel:/HEAD/standard/",
        "package": "kernel-default",
        "chained": True,
        "o3_group_id": 32,
        "base_test": "install_ltp+opensuse+DVD",
        "base_key_map": BASE_HDD_KEY_MAP,
        "machine": "64bit",
        "extra_chains": [
            {"test": "create_hdd_xfstests", "key_map": XFSTESTS_HDD_KEY_MAP},
        ],
    },
    "aarch64": {
        "kotd_repo": "https://download.opensuse.org/repositories/Kernel:/HEAD/ARM/",
        "package": "kernel-default",
        "chained": True,
        "o3_group_id": 32,
        "base_test": "install_ltp+opensuse+DVD",
        "base_key_map": BASE_HDD_KEY_MAP_UEFI,
        "machine": "aarch64",
        "extra_chains": [
            {"test": "create_hdd_xfstests", "key_map": XFSTESTS_HDD_KEY_MAP_UEFI},
        ],
    },
    "ppc64le": {
        "kotd_repo": "https://download.opensuse.org/repositories/Kernel:/HEAD/PPC/",
        "package": "kernel-default",
        "chained": True,
        "o3_group_id": 32,
        "base_test": "install_ltp+opensuse+DVD",
        "base_key_map": BASE_HDD_KEY_MAP,
        "machine": "ppc64le",
        "extra_chains": [
            {"test": "create_hdd_xfstests", "key_map": XFSTESTS_HDD_KEY_MAP},
        ],
    },
    "s390x": {
        "kotd_repo": "https://download.opensuse.org/repositories/Kernel:/HEAD/S390/",
        "package": "kernel-default",
        "chained": False,
        "o3_group_id": 32,
        "media_test": "ltp_syscalls",
    },
}

S390X_MEDIA_KEYS = [
    "DVD", "FULLURL", "ISO", "ISO_MAXSIZE", "ASSET_256", "CHECKSUM_ISO",
    "INST_INSTALL_URL", "MIRROR_HTTP", "MIRROR_HTTPS", "MIRROR_PREFIX", "SUSEMIRROR",
    "REPO_0", "REPO_1", "REPO_2", "REPO_3",
    "REPO_OSS", "REPO_NON_OSS", "REPO_OSS_DEBUG", "REPO_OSS_DEBUG_PACKAGES",
    "REPO_OSS_SOURCE", "REPO_OSS_SOURCE_PACKAGES",
]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    tmp_path = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp_path, STATE_FILE)


def get_repo_metadata(repo_url: str) -> tuple[str, str]:
    r = requests.get(repo_url.rstrip("/") + "/repodata/repomd.xml", timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    revision = root.find("r:revision", REPOMD_NS).text
    primary_href = None
    for data in root.findall("r:data", REPOMD_NS):
        if data.get("type") == "primary":
            primary_href = data.find("r:location", REPOMD_NS).get("href")
    if not primary_href:
        raise ValueError(f"no primary data found in repomd.xml for {repo_url}")
    return revision, repo_url.rstrip("/") + "/" + primary_href


def get_kernel_build(primary_xml_url: str, arch: str, package: str) -> str:
    r = requests.get(primary_xml_url, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(gzip.decompress(r.content))
    for pkg in root.findall("c:package", PRIMARY_NS):
        if pkg.find("c:name", PRIMARY_NS).text != package:
            continue
        if pkg.find("c:arch", PRIMARY_NS).text != arch:
            continue
        ver_el = pkg.find("c:version", PRIMARY_NS)
        ver, rel, epoch = ver_el.get("ver"), ver_el.get("rel"), ver_el.get("epoch")
        build = f"{ver}-{rel}"
        return f"{epoch}:{build}" if epoch and epoch != "0" else build
    raise ValueError(f"{package} ({arch}) not found in {primary_xml_url}")


def resolve_hdd_settings(
    client: OpenQA_Client,
    group_id: int,
    arch: str,
    machine: str,
    test: str,
    key_map: dict[str, str],
) -> tuple[str, dict] | tuple[None, None]:
    jobs = client.openqa_request(
        "GET",
        "jobs",
        params={
            "groupid": group_id,
            "test": test,
            "arch": arch,
            "machine": machine,
            "result": "passed",
            "limit": 1,
        },
    )["jobs"]
    if not jobs:
        return None, None

    detail = client.openqa_request("GET", f"jobs/{jobs[0]['id']}")["job"]
    settings = detail["settings"]
    hdds = detail.get("assets", {}).get("hdd", [])
    primary_key = next(iter(key_map))
    primary_val = settings.get(primary_key)
    if primary_val and primary_val in hdds:
        resolved = {dest: settings[src] for src, dest in key_map.items() if src in settings}
        return settings["BUILD"], resolved
    log.warning("%s '%s' for job %s not found in current assets", primary_key, primary_val, jobs[0]["id"])
    return None, None


def resolve_s390x_media(client: OpenQA_Client, group_id: int, test: str) -> dict | None:
    jobs = client.openqa_request(
        "GET",
        "jobs",
        params={"groupid": group_id, "test": test, "arch": "s390x", "limit": 1},
    )["jobs"]
    if not jobs:
        return None

    detail = client.openqa_request("GET", f"jobs/{jobs[0]['id']}")["job"]
    assets = detail.get("assets", {})
    if assets.get("iso") and assets.get("repo"):
        settings = detail["settings"]
        return {k: settings[k] for k in S390X_MEDIA_KEYS if k in settings}
    log.warning("Job %s has no iso/repo assets", jobs[0]["id"])
    return None


def trigger_kotd(
    client: OpenQA_Client,
    arch: str,
    build: str,
    commit: bool,
    extra_settings: dict | None = None,
) -> None:
    """POST an `isos post` request to openQA for group 140, scoped to `arch`.

    Always defines:
        DISTRI=opensuse, VERSION=Tumbleweed, ARCH=<arch>, FLAVOR=KOTD,
        BUILD=<resolved kernel build>, _GROUP_ID=140

    `extra_settings` (built by main(), merged in on top) additionally carries,
    depending on arch:
        BASE_HDD_1                    - base image install_ltp+opensuse+KOTD boots from
        BASE_UEFI_PFLASH_VARS         - its paired pflash-vars image (aarch64 only)
        XFSTESTS_BASE_HDD_1           - base image install_kotd_xfstests boots from
        XFSTESTS_BASE_UEFI_PFLASH_VARS - its paired pflash-vars image (aarch64 only)
        DVD, FULLURL, ISO, ISO_MAXSIZE, ASSET_256, CHECKSUM_ISO,
        INST_INSTALL_URL, MIRROR_HTTP, MIRROR_HTTPS, MIRROR_PREFIX,
        SUSEMIRROR, REPO_0, REPO_1, REPO_2, REPO_3, REPO_OSS, REPO_NON_OSS,
        REPO_OSS_DEBUG, REPO_OSS_DEBUG_PACKAGES, REPO_OSS_SOURCE,
        REPO_OSS_SOURCE_PACKAGES       - s390x install media/repo settings only
    """
    settings = {
        "DISTRI": "opensuse",
        "VERSION": "Tumbleweed",
        "ARCH": arch,
        "FLAVOR": "KOTD",
        "BUILD": build,
        "_GROUP_ID": GROUP_ID,
    }
    if extra_settings:
        settings.update(extra_settings)
    cmd = "openqa-cli api --host {} -X POST isos {}".format(
        OPENQA_HOST, " ".join(f"{k}={v}" for k, v in settings.items())
    )
    if not commit:
        log.info("[dry-run] would run: %s", cmd)
        return
    log.info("Posting: %s", cmd)
    client.openqa_request("POST", "isos", data=settings)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true", help="Actually post to openQA (default: dry run)")
    parser.add_argument("--arch", choices=ARCHES.keys(), help="Only check this architecture")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    state = load_state()
    client = OpenQA_Client(server=OPENQA_HOST)

    arches = {args.arch: ARCHES[args.arch]} if args.arch else ARCHES

    for arch, cfg in arches.items():
        log.info("Checking %s (%s)", arch, cfg["kotd_repo"])
        revision, primary_xml_url = get_repo_metadata(cfg["kotd_repo"])
        last_seen = state.get(arch, {}).get("revision")

        if revision == last_seen:
            log.info("%s: no new kernel build (revision %s unchanged)", arch, revision)
            continue

        log.info("%s: new kernel build detected (revision %s -> %s)", arch, last_seen, revision)

        build = get_kernel_build(primary_xml_url, arch, cfg["package"])
        log.info("%s: kernel build is %s", arch, build)

        base_build = None
        extra_settings: dict = {}
        if cfg["chained"]:
            base_build, base_settings = resolve_hdd_settings(
                client, cfg["o3_group_id"], arch, cfg["machine"], cfg["base_test"], cfg["base_key_map"]
            )
            if not base_settings:
                log.error("%s: could not resolve a usable base HDD, skipping trigger", arch)
                continue
            log.info("%s: using base HDD %s (from build %s)", arch, base_settings.get("BASE_HDD_1"), base_build)
            extra_settings.update(base_settings)
        elif not cfg["chained"]:
            media = resolve_s390x_media(client, cfg["o3_group_id"], cfg["media_test"])
            if not media:
                log.error("%s: could not resolve install media, skipping trigger", arch)
                continue
            log.info("%s: using install media %s", arch, media.get("ISO"))
            extra_settings.update(media)

        skip_arch = False
        for chain in cfg.get("extra_chains", []):
            chain_build, chain_settings = resolve_hdd_settings(
                client, cfg["o3_group_id"], arch, cfg["machine"], chain["test"], chain["key_map"]
            )
            if not chain_settings:
                log.error("%s: could not resolve %s, skipping trigger", arch, chain["test"])
                skip_arch = True
                break
            log.info("%s: using %s -> %s (from build %s)", arch, chain["test"], chain_settings, chain_build)
            extra_settings.update(chain_settings)
        if skip_arch:
            continue

        trigger_kotd(client, arch, build, args.commit, extra_settings)

        if args.commit:
            state[arch] = {"revision": revision, "build": build}
            if base_build is not None:
                state[arch]["base_build"] = base_build
            save_state(state)


if __name__ == "__main__":
    main()
