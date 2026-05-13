# Kubernetes deployment

Kustomize manifests for the **agent side** of multi-agent-commag —
InfluxDB, Ollama (with one-shot init Job), and the agents Pod
(L2 Manager + Scheduler Agent sharing `subintents.json` via an
emptyDir).

This mirrors the agent-side services in `deploy/docker-compose.yml`.
The OAI core/RAN side is out of scope here; point `GNB_API_URL` at a
reachable OAI gNB via an overlay.

## Layout

```
base/                      generic, vendor-neutral manifests
overlays/openshift-dev/    used to test on a dedicated OpenShift namespace
overlays/public-example/   minimal reference for vanilla Kubernetes
```

Real values live in `secret-patch.yaml` and `configmap-patch.yaml`
under each overlay. `.example` templates are committed; the real files
are gitignored.

The image is intentionally a bare name in `base/`; each overlay sets a
real registry path via its own `images:` directive (kustomize matches
by the original name).

## Apply

```
kubectl kustomize deploy/k8s/overlays/<name> | kubectl apply -f -
```

On OpenShift, swap `kubectl` for `oc`. The `ollama-init` Job pulls the
model and runs a warm-up inference; the agents Pod's `wait-for-ollama`
initContainer blocks until the model is reachable.

GPU: the Ollama StatefulSet requests one `nvidia.com/gpu`. On CPU-only
clusters, patch this out in your overlay.
