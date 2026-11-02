import os
import re
import time

import httpx
from fastapi import FastAPI

NAMESPACE = "x-ui"
DEPLOYMENT = "x-ui"
CONTAINER = "primary"
LOCK_ANNOTATION = "socrat.alchemmist.xyz/upgrade-lock"
IMAGE_PREFIX = "ghcr.io/mhsanaei/3x-ui"
GITHUB_RELEASE = "https://api.github.com/repos/MHSanaei/3x-ui/releases/latest"
_TAG = re.compile(r"[A-Za-z0-9_.-]{1,128}")

_cluster_override = None


class K8sError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def image_tag(image: str) -> str:
    if "@sha256:" in image:
        return ""
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[colon + 1 :]
    return ""


def _version_key(tag: str) -> tuple[int, ...] | None:
    raw = tag[1:] if tag[:1] in {"v", "V"} else tag
    parts: list[int] = []
    for piece in raw.split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def is_newer(installed: str, latest: str) -> bool:
    installed_tag = image_tag(installed)
    latest_tag = image_tag(latest)
    if not latest_tag:
        return False
    left = _version_key(installed_tag)
    right = _version_key(latest_tag)
    if left is not None and right is not None:
        return left < right
    return installed_tag != latest_tag


def container_image(deployment: dict) -> str:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container.get("name") == CONTAINER and container.get("image"):
            return container["image"]
    raise RuntimeError("container primary not found on deployment x-ui")


def read_lock(deployment: dict) -> str | None:
    annotations = (deployment.get("metadata") or {}).get("annotations") or {}
    return annotations.get(LOCK_ANNOTATION)


def lock_patch(deployment: dict) -> list[dict]:
    resource_version = deployment["metadata"]["resourceVersion"]
    ops: list[dict] = [
        {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version}
    ]
    annotations = (deployment.get("metadata") or {}).get("annotations")
    if not annotations:
        ops.append(
            {
                "op": "add",
                "path": "/metadata/annotations",
                "value": {LOCK_ANNOTATION: "running"},
            }
        )
    else:
        ops.append(
            {
                "op": "add",
                "path": "/metadata/annotations/" + _pointer(LOCK_ANNOTATION),
                "value": "running",
            }
        )
    return ops


def unlock_patch() -> list[dict]:
    return [
        {
            "op": "add",
            "path": "/metadata/annotations/" + _pointer(LOCK_ANNOTATION),
            "value": "done",
        }
    ]


def image_patch(image: str) -> dict:
    return {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": CONTAINER, "image": image}]}
            }
        }
    }


def panel_health_url() -> str:
    port = os.environ.get("XUI_PANEL_PORT", "2053")
    path = os.environ.get("XUI_HEALTH_PATH", "/")
    if not path.startswith("/"):
        path = "/" + path
    return f"http://x-ui.x-ui.svc.cluster.local:{port}{path}"


def latest_release_image() -> str:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "socrat-bot"}
    with httpx.Client(timeout=20) as client:
        response = client.get(GITHUB_RELEASE, headers=headers)
        response.raise_for_status()
        tag = str(response.json().get("tag_name", "")).strip()
    if not _TAG.fullmatch(tag):
        raise RuntimeError("unexpected GitHub release tag")
    return f"{IMAGE_PREFIX}:{tag}"


def panel_healthy() -> bool:
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(panel_health_url())
            return response.is_success
    except httpx.HTTPError:
        return False


class InCluster:
    def _request(self, method: str, path: str, **kwargs):
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        token = open(token_path, encoding="utf-8").read().strip()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        url = f"https://{host}:{port}{path}"
        with httpx.Client(verify=ca, timeout=30) as client:
            response = client.request(method, url, headers=headers, **kwargs)
        if response.status_code >= 400:
            detail = response.text[:500]
            raise K8sError(
                f"kubernetes {method} {path} failed with {response.status_code}: {detail}",
                response.status_code,
            )
        if not response.content:
            return {}
        return response.json()

    def read_deployment(self) -> dict:
        return self._request(
            "GET", f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}"
        )

    def patch_json(self, ops: list[dict]) -> dict:
        return self._request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}",
            json=ops,
            headers={"Content-Type": "application/json-patch+json"},
        )

    def patch_strategic(self, body: dict) -> dict:
        return self._request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT}",
            json=body,
            headers={"Content-Type": "application/strategic-merge-patch+json"},
        )

    def list_pods(self, selector: str) -> list[dict]:
        payload = self._request(
            "GET",
            f"/api/v1/namespaces/{NAMESPACE}/pods",
            params={"labelSelector": selector},
        )
        return payload.get("items") or []


def cluster():
    if _cluster_override is not None:
        return _cluster_override
    return InCluster()


def rollout_ready(target: str) -> bool:
    deployment = cluster().read_deployment()
    if container_image(deployment) != target:
        return False
    replicas = int((deployment.get("spec") or {}).get("replicas") or 1)
    status = deployment.get("status") or {}
    generation = int((deployment.get("metadata") or {}).get("generation") or 0)
    if int(status.get("observedGeneration") or 0) < generation:
        return False
    if int(status.get("updatedReplicas") or 0) != replicas:
        return False
    if int(status.get("availableReplicas") or 0) != replicas:
        return False
    if int(status.get("unavailableReplicas") or 0) != 0:
        return False
    labels = ((deployment.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    if not labels:
        return False
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    ready = 0
    for pod in cluster().list_pods(selector):
        metadata = pod.get("metadata") or {}
        if metadata.get("deletionTimestamp"):
            continue
        containers = (pod.get("spec") or {}).get("containers") or []
        primary = [item.get("image") for item in containers if item.get("name") == CONTAINER]
        phase = (pod.get("status") or {}).get("phase")
        if primary != [target]:
            if phase in {"Pending", "Running"}:
                return False
            continue
        conditions = (pod.get("status") or {}).get("conditions") or []
        if any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions):
            ready += 1
        elif phase == "Running":
            return False
    return ready >= replicas


def _wait_healthy(target: str) -> bool:
    timeout = float(os.environ.get("XUI_UPGRADE_TIMEOUT_SECONDS", "180"))
    interval = float(os.environ.get("XUI_POLL_SECONDS", "2"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rollout_ready(target) and panel_healthy():
            return True
        time.sleep(interval)
    return False


def _acquire(api_client) -> bool:
    deployment = api_client.read_deployment()
    if read_lock(deployment) == "running":
        return False
    try:
        api_client.patch_json(lock_patch(deployment))
    except K8sError as exc:
        if exc.status_code == 409:
            return False
        raise
    return True


def _release(api_client) -> None:
    api_client.patch_json(unlock_patch())


def _rollout(api_client, installed: str) -> dict:
    changed = False
    try:
        latest = latest_release_image()
        if not is_newer(installed, latest):
            return {"status": "current", "image": installed}
        api_client.patch_strategic(image_patch(latest))
        changed = True
        if _wait_healthy(latest):
            return {"status": "updated", "image": latest}
        api_client.patch_strategic(image_patch(installed))
        return {
            "status": "rolled_back",
            "image": installed,
            "message": (
                "New 3X-UI image did not become Ready and the panel health check did not succeed. "
                "Restored the previous image."
            ),
        }
    except Exception as exc:
        if not changed:
            return {
                "status": "rolled_back",
                "image": installed,
                "message": (
                    f"Upgrade failed before the image changed ({exc}). "
                    "The running image was left in place."
                ),
            }
        try:
            api_client.patch_strategic(image_patch(installed))
        except Exception as rollback_exc:
            return {
                "status": "rolled_back",
                "image": installed,
                "message": (
                    f"Upgrade failed ({exc}). Rollback to the previous image failed ({rollback_exc})."
                ),
            }
        return {
            "status": "rolled_back",
            "image": installed,
            "message": f"Upgrade failed ({exc}). Restored the previous image.",
        }


api = FastAPI()


@api.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@api.get("/version")
def version() -> dict:
    installed = container_image(cluster().read_deployment())
    latest = latest_release_image()
    return {"installed": installed, "latest": latest, "newer": is_newer(installed, latest)}


@api.post("/upgrade")
def upgrade() -> dict:
    api_client = cluster()
    deployment = api_client.read_deployment()
    installed = container_image(deployment)
    if read_lock(deployment) == "running":
        return {"status": "running", "image": installed}
    if not _acquire(api_client):
        deployment = api_client.read_deployment()
        return {"status": "running", "image": container_image(deployment)}
    try:
        return _rollout(api_client, installed)
    finally:
        _release(api_client)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("socrat_bot.updater:api", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
