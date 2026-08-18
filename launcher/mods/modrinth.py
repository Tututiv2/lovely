"""Modrinth's public API: search for mods, and install them into one instance.

Modrinth is used rather than CurseForge because its v2 API needs no key, no signup and no
scraping, and it publishes a SHA-1 for every file -- which slots straight into the same
verify-before-use rule the rest of the launcher follows.

Two things shape this module:

**A mod is only ever installed into one instance's own ``mods`` folder.** There is no
"global" install and there cannot be one. That is the whole premise of the launcher, and
it is enforced by taking an :class:`~launcher.instances.Instance` rather than a path.

**Compatibility is filtered server-side.** The search is faceted on the instance's exact
Minecraft version and loader, so an incompatible mod is never offered in the first place.
Filtering after the fact would be the same amount of code and would still let someone
install a Fabric mod into a Forge instance if the list were stale.

Downloads are restricted to Modrinth's own CDN (:data:`ALLOWED_HOSTS`). The API returns a
URL and it would be easy to fetch whatever it says; pinning the host means a compromised or
spoofed index cannot turn "install a mod" into "run an arbitrary download".
"""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .. import logs, net
from ..instances import Instance
from ..paths import APP_NAME, APP_VERSION, ext

log = logs.get("mods.modrinth")

API = "https://api.modrinth.com/v2"

#: Modrinth asks for a descriptive User-Agent (project, version, contact) and will
#: rate-limit anonymous junk. The contact is the public repository.
PROJECT_URL = "https://github.com/Tututiv2/lovely"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} ({PROJECT_URL})"

#: Files are only ever fetched from these hosts, whatever the API hands back.
ALLOWED_HOSTS = ("cdn.modrinth.com", "cdn-raw.modrinth.com")

PAGE_SIZE = 20


class ModrinthError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------------

@dataclass
class ModProject:
    project_id: str
    slug: str
    title: str
    description: str
    downloads: int
    follows: int
    categories: list[str] = field(default_factory=list)
    icon_url: str = ""
    author: str = ""
    project_type: str = "mod"

    @property
    def downloads_short(self) -> str:
        n = self.downloads
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
        if n >= 1000:
            return f"{n / 1000:.0f}k"
        return str(n)


@dataclass
class ModFile:
    filename: str
    url: str
    sha1: str
    size: int
    primary: bool = True


@dataclass
class ModVersion:
    version_id: str
    project_id: str
    name: str
    version_number: str
    game_versions: list[str]
    loaders: list[str]
    files: list[ModFile]
    dependencies: list[dict] = field(default_factory=list)
    date_published: str = ""

    @property
    def primary_file(self) -> ModFile | None:
        for f in self.files:
            if f.primary:
                return f
        return self.files[0] if self.files else None

    def required_project_ids(self) -> list[str]:
        return [d["project_id"] for d in self.dependencies
                if d.get("dependency_type") == "required" and d.get("project_id")]


# ---------------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------------

def _get(path: str, params: dict | None = None) -> dict | list:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    raw = net.get_bytes(url, headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/json"})
    return json.loads(raw.decode("utf-8"))


def search(query: str = "", *, mc_version: str = "", loader: str = "",
           project_type: str = "mod", limit: int = PAGE_SIZE,
           offset: int = 0) -> tuple[list[ModProject], int]:
    """Search Modrinth. Returns (results, total hits).

    Facets are AND-ed across the outer list and OR-ed within each inner list, so this asks
    for "this project type AND this game version AND this loader".
    """
    facets: list[list[str]] = [[f"project_type:{project_type}"]]
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if loader and loader != "vanilla":
        facets.append([f"categories:{loader}"])

    params = {"limit": max(1, min(100, limit)), "offset": max(0, offset),
              "index": "relevance" if query.strip() else "downloads",
              "facets": json.dumps(facets)}
    if query.strip():
        params["query"] = query.strip()

    data = _get("/search", params)
    hits = data.get("hits", []) if isinstance(data, dict) else []
    results = [
        ModProject(
            project_id=h.get("project_id", ""),
            slug=h.get("slug", ""),
            title=h.get("title", "") or h.get("slug", ""),
            description=(h.get("description") or "").strip(),
            downloads=int(h.get("downloads") or 0),
            follows=int(h.get("follows") or 0),
            categories=list(h.get("categories") or []),
            icon_url=h.get("icon_url") or "",
            author=h.get("author") or "",
            project_type=h.get("project_type") or project_type,
        )
        for h in hits
    ]
    total = int(data.get("total_hits", len(results))) if isinstance(data, dict) else 0
    return results, total


def _parse_version(v: dict) -> ModVersion:
    files = [
        ModFile(filename=f.get("filename", ""), url=f.get("url", ""),
                sha1=(f.get("hashes") or {}).get("sha1", ""),
                size=int(f.get("size") or 0), primary=bool(f.get("primary")))
        for f in (v.get("files") or [])
    ]
    if files and not any(f.primary for f in files):
        files[0].primary = True
    return ModVersion(
        version_id=v.get("id", ""),
        project_id=v.get("project_id", ""),
        name=v.get("name", ""),
        version_number=v.get("version_number", ""),
        game_versions=list(v.get("game_versions") or []),
        loaders=list(v.get("loaders") or []),
        files=files,
        dependencies=list(v.get("dependencies") or []),
        date_published=v.get("date_published", ""),
    )


def versions(project_id: str, *, mc_version: str = "",
             loader: str = "") -> list[ModVersion]:
    """Compatible releases for a project, newest first."""
    params = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    if loader and loader != "vanilla":
        params["loaders"] = json.dumps([loader])
    data = _get(f"/project/{project_id}/version", params or None)
    return [_parse_version(v) for v in data] if isinstance(data, list) else []


def best_version(project_id: str, *, mc_version: str, loader: str) -> ModVersion | None:
    found = versions(project_id, mc_version=mc_version, loader=loader)
    return found[0] if found else None


def project(project_id: str) -> ModProject | None:
    try:
        p = _get(f"/project/{project_id}")
    except net.NetError:
        return None
    if not isinstance(p, dict):
        return None
    return ModProject(
        project_id=p.get("id", project_id), slug=p.get("slug", ""),
        title=p.get("title", ""), description=(p.get("description") or "").strip(),
        downloads=int(p.get("downloads") or 0), follows=int(p.get("followers") or 0),
        categories=list(p.get("categories") or []), icon_url=p.get("icon_url") or "",
        project_type=p.get("project_type") or "mod")


# ---------------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------------

def check_url(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ModrinthError(
            f"Refusing to download from {host or 'an empty host'}: mod files are only "
            f"fetched from Modrinth's CDN.")
    return url


@dataclass
class InstallResult:
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        bits = []
        if self.installed:
            bits.append(f"{len(self.installed)} installed")
        if self.skipped:
            bits.append(f"{len(self.skipped)} already present")
        if self.failed:
            bits.append(f"{len(self.failed)} failed")
        return ", ".join(bits) or "nothing to do"


def install(instance: Instance, version: ModVersion, *,
            with_dependencies: bool = True,
            progress: net.Progress | None = None,
            cancel: net.CancelToken = net.NEVER,
            _seen: set[str] | None = None,
            _result: InstallResult | None = None) -> InstallResult:
    """Download a mod (and anything it requires) into this instance's own mods folder."""
    result = _result if _result is not None else InstallResult()
    seen = _seen if _seen is not None else set()
    if version.project_id in seen:
        return result
    seen.add(version.project_id)

    mods_dir = instance.mods_dir
    mods_dir.mkdir(parents=True, exist_ok=True)

    file = version.primary_file
    if file is None or not file.url:
        result.failed.append((version.name or version.project_id, "no downloadable file"))
        return result

    dest = mods_dir / Path(file.filename).name       # never trust a path from an API
    label = f"{version.name or version.version_number} ({file.filename})"
    if progress:
        progress.set_phase("Installing mods", file.filename)

    try:
        check_url(file.url)
        if net.verify(dest, file.sha1 or None, file.size or None):
            result.skipped.append(file.filename)
        else:
            net.download(file.url, dest, sha1=file.sha1 or None,
                         size=file.size or None, cancel=cancel,
                         on_bytes=progress.add_bytes if progress else None)
            result.installed.append(file.filename)
            log.info("installed %s into %s", file.filename, instance.slug)
    except (net.NetError, ModrinthError, OSError) as exc:
        result.failed.append((label, str(exc)[:120]))
        return result

    if with_dependencies:
        for dep_id in version.required_project_ids():
            if dep_id in seen:
                continue
            cancel.check()
            try:
                dep = best_version(dep_id, mc_version=instance.mc_version,
                                   loader=instance.loader)
            except net.NetError as exc:
                result.failed.append((dep_id, f"could not resolve dependency: {exc}"))
                continue
            if dep is None:
                result.failed.append(
                    (dep_id, f"no build for {instance.mc_version} {instance.loader}"))
                continue
            install(instance, dep, with_dependencies=True, progress=progress,
                    cancel=cancel, _seen=seen, _result=result)

    return result


def installed_filenames(instance: Instance) -> set[str]:
    try:
        return {p.name for p in instance.mods_dir.glob("*.jar")}
    except OSError:
        return set()
