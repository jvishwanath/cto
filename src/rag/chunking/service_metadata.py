"""
Service-level metadata extractor.
Scans application.properties and Java source for cross-service dependencies.
Produces per-repo metadata that gets attached to every chunk from that repo.
"""

import os
import re
from pathlib import Path

# Patterns to detect service URLs in properties files
_SERVICE_URL_PATTERN = re.compile(
    r"^app\.(\w[\w.-]*)\.service[_-]?url\s*=\s*(.+)$", re.MULTILINE
)
_CRYPTO_URL_PATTERN = re.compile(
    r"^app\.crypto\.service-url\s*=\s*(.+)$", re.MULTILINE
)

# Patterns to detect outbound HTTP calls in Java
_REST_CALL_PATTERNS = [
    re.compile(r'(?:restTemplate|httpClient|webClient)\.\w+\(.*?"([^"]*?/api/[^"]*?)"', re.DOTALL),
    re.compile(r'@FeignClient\s*\(\s*.*?url\s*=\s*"([^"]*)"', re.DOTALL),
    re.compile(r'@FeignClient\s*\(\s*.*?name\s*=\s*"([^"]*)"', re.DOTALL),
]

# Patterns to detect Spring Value injections of service URLs
_VALUE_URL_PATTERN = re.compile(
    r'@Value\s*\(\s*"\$\{app\.(\w[\w.-]*)\.service[_-]?url\}"'
)

# Known service name mapping (hostname patterns → logical service names)
_KNOWN_SERVICES = {
    "crypto": "crypto-service",
    "authn": "authn-service",
    "authz": "authz-service",
    "ratelimit": "ratelimit-service",
    "jwks": "jwks-service",
    "otel": "otel-collector",
    "metrics": "metrics-service",
    "wally": "wally-service",
    "statslog": "statslog-service",
}


def metadata_source_patterns(repo_path: Path) -> list[str]:
    """
    File patterns whose changes invalidate this repo's service metadata
    (depends_on, layer, etc.). Auto-detected per build system; always
    includes the manual override file. Used by the incremental indexer
    to decide when to escalate file-level → full repo rebuild.
    """
    patterns = [".ragmeta.yaml", ".ragmeta.yml"]

    if (repo_path / "build.gradle").exists() or (repo_path / "pom.xml").exists() \
       or any(repo_path.rglob("application.properties")):
        patterns += ["**/application.properties", "**/application*.yml",
                     "**/application*.yaml", "*.gradle", "build.gradle*", "pom.xml"]
    if (repo_path / "go.mod").exists():
        patterns += ["go.mod", "go.sum"]
    if (repo_path / "package.json").exists():
        patterns += ["package.json", "package-lock.json", "yarn.lock"]
    if (repo_path / "Cargo.toml").exists():
        patterns += ["Cargo.toml", "Cargo.lock"]
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        patterns += ["pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt"]
    if any(repo_path.rglob("Chart.yaml")):
        patterns += ["**/Chart.yaml", "**/values.yaml", "**/values-*.yaml"]

    return patterns


def extract_repo_metadata(repo_path: Path) -> dict:
    """
    Scan a repo for service dependencies.
    Returns metadata dict to attach to all chunks from this repo.
    """
    repo_name = repo_path.name
    service_name = _derive_service_name(repo_name)
    depends_on = set()
    service_urls = {}
    exposed_endpoints = []

    # Scan all properties files
    for props_file in repo_path.rglob("application.properties"):
        deps, urls = _parse_properties(props_file)
        depends_on.update(deps)
        service_urls.update(urls)

    # Scan Java source for REST controller endpoints (what this service exposes)
    for java_file in repo_path.rglob("*.java"):
        if "test" in str(java_file):
            continue
        try:
            content = java_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # Find exposed endpoints
        endpoints = _extract_exposed_endpoints(content)
        exposed_endpoints.extend(endpoints)

        # Find injected service URLs (confirms dependency)
        injected = _VALUE_URL_PATTERN.findall(content)
        for svc_key in injected:
            base_name = svc_key.split(".")[0]
            if base_name in _KNOWN_SERVICES:
                depends_on.add(_KNOWN_SERVICES[base_name])

    return {
        "service_name": service_name,
        "depends_on": sorted(depends_on),
        "service_urls": service_urls,
        "exposed_endpoints": sorted(set(exposed_endpoints))[:20],
    }


def extract_file_metadata(filepath: str, text: str, repo_metadata: dict) -> dict:
    """
    Per-file metadata overlay: detect if this specific file makes outbound calls.
    """
    calls_services = set()

    # Check for service URL references in this file
    for svc_key, svc_name in _KNOWN_SERVICES.items():
        if svc_key.lower() in text.lower() and svc_name in repo_metadata.get("depends_on", []):
            calls_services.add(svc_name)

    # Detect layer from path
    layer = _detect_layer(filepath)

    return {
        "calls_services": sorted(calls_services),
        "layer": layer,
    }


def build_context_prefix(chunk_meta: dict, repo_meta: dict, file_meta: dict) -> str:
    """
    Build a rich context prefix to prepend to chunk text before embedding.
    This injects service relationship info directly into the embedding.
    """
    parts = []
    parts.append(f"// Service: {repo_meta['service_name']}")

    if file_meta.get("layer"):
        parts.append(f"Layer: {file_meta['layer']}")

    if file_meta.get("calls_services"):
        parts.append(f"Calls: {', '.join(file_meta['calls_services'])}")

    if repo_meta.get("depends_on"):
        parts.append(f"Depends on: {', '.join(repo_meta['depends_on'][:5])}")

    return " | ".join(parts)


_SERVICE_PREFIXES = tuple(
    p for p in os.environ.get("SERVICE_NAME_PREFIXES", "").split(",")
    if p.strip())


def _derive_service_name(repo_name: str) -> str:
    """Convert repo name to logical service name. If
    SERVICE_NAME_PREFIXES is set (comma-separated), strip the
    first matching prefix (e.g. an org-wide repo prefix)."""
    name = repo_name
    for pfx in _SERVICE_PREFIXES:
        if name.startswith(pfx):
            name = name[len(pfx):]
            break
    return f"{name}-service"


def _parse_properties(props_file: Path) -> tuple[set, dict]:
    """Parse application.properties for service-url entries."""
    depends_on = set()
    service_urls = {}

    try:
        content = props_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return depends_on, service_urls

    for match in _SERVICE_URL_PATTERN.finditer(content):
        key_path = match.group(1)  # e.g., "crypto", "ratelimit", "jwks"
        url = match.group(2).strip()

        base_name = key_path.split(".")[0]
        if base_name in _KNOWN_SERVICES:
            logical_name = _KNOWN_SERVICES[base_name]
            depends_on.add(logical_name)
            service_urls[logical_name] = url

    # Also check for crypto URL pattern specifically
    crypto_match = _CRYPTO_URL_PATTERN.search(content)
    if crypto_match:
        depends_on.add("crypto-service")
        service_urls["crypto-service"] = crypto_match.group(1).strip()

    return depends_on, service_urls


def _extract_exposed_endpoints(java_content: str) -> list[str]:
    """Find REST endpoints this service exposes."""
    endpoints = []
    patterns = [
        re.compile(r'@(?:Get|Post|Put|Delete|Patch)Mapping\s*\(\s*"([^"]*)"'),
        re.compile(r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?"([^"]*)"'),
        re.compile(r'@RequestMapping\s*\(\s*path\s*=\s*"([^"]*)"'),
    ]
    for pat in patterns:
        endpoints.extend(pat.findall(java_content))
    return endpoints


def _detect_layer(filepath: str) -> str:
    """Infer architectural layer from file path."""
    path_lower = filepath.lower()
    if "controller" in path_lower:
        return "controller"
    elif "service/impl" in path_lower:
        return "service-impl"
    elif "service" in path_lower:
        return "service"
    elif "filter" in path_lower:
        return "filter"
    elif "config" in path_lower:
        return "config"
    elif "model" in path_lower or "dto" in path_lower or "entity" in path_lower:
        return "model"
    elif "util" in path_lower or "helper" in path_lower:
        return "util"
    elif "repository" in path_lower or "dao" in path_lower:
        return "repository"
    elif "wally" in path_lower:
        return "cache"
    elif "exception" in path_lower:
        return "exception"
    return "other"
