#!/usr/bin/env bash
# Single-shot, idempotent driver for an end-to-end multi-agent-commag
# deploy on OpenShift (agents + Ollama + InfluxDB + OAI CN + OAI RAN +
# 3 agentic-loop intent tests).
#
# Pre-requisites the script verifies:
#   - oc logged in
#   - OAI submodule checked out at the SHA pinned by HEAD
#   - Docker Hub pull-secret exists in the namespace (or DOCKERHUB_USER
#     and DOCKERHUB_TOKEN env vars are set so the script can create it)
#
# Usage (from the repo root):
#   NS=my-namespace \
#   DOCKERHUB_USER=… DOCKERHUB_TOKEN=… \
#   deploy/k8s/openshift-deploy.sh
#
# Or pre-create the docker-hub secret yourself and run without the env vars.
#
# Outputs:
#   /tmp/<NS>-deploy.log         full structured log
#   /tmp/<NS>-intents/<name>.log per-intent scheduler decisions + L2 sub-intent
set -uo pipefail

NS=${NS:?set NS to the target OpenShift namespace}
# Find the repo: explicit REPO env wins; otherwise walk up from $PWD
# looking for the .gitmodules that lists the oai submodule.
if [ -z "${REPO:-}" ]; then
  d=$PWD
  while [ "$d" != "/" ]; do
    if [ -f "$d/.gitmodules" ] && grep -q '^\[submodule "oai"\]' "$d/.gitmodules"; then
      REPO=$d; break
    fi
    d=$(dirname "$d")
  done
fi
[ -n "${REPO:-}" ] && [ -d "$REPO/.git" ] \
  || { echo "[FAIL] can't find multi-agent-commag repo; set REPO=/abs/path" >&2; exit 1; }

LOG=/tmp/${NS}-deploy.log
INTENTS_DIR=/tmp/${NS}-intents

cd "$REPO"
exec > >(tee -a "$LOG") 2>&1
mkdir -p "$INTENTS_DIR"

H()    { echo ""; echo "==[$(date +%H:%M:%S)]== $*"; }
fail() { echo "[FAIL] $*" >&2; exit 1; }
# Retry oc on transient API errors (TLS/DNS/timeout). Non-transient failures
# pass through after one try by checking the error message.
ocr() {
  for i in 1 2 3 4 5 6; do
    if out=$(oc "$@" 2>&1); then echo "$out"; return 0; fi
    case "$out" in
      *"AlreadyExists"*|*"already exists"*|*"already mapped"*) echo "$out"; return 0 ;;
      *"NotFound"*|*"not found"*) echo "$out" >&2; return 1 ;;
    esac
    echo "[ocr retry $i/6] $*" >&2
    echo "  -> $out" >&2
    sleep $((i * 5))
  done
  echo "$out" >&2
  return 1
}

H "Pre-flight: oc auth"
ocr whoami >/dev/null || fail "not logged in to OpenShift"

H "Pre-flight: OAI submodule at HEAD's pinned SHA"
expected=$(git ls-tree HEAD oai | awk '{print $3}')
got=$(git -C oai rev-parse HEAD 2>/dev/null || true)
if [ "$got" != "$expected" ]; then
  echo "submodule at $got, need $expected — updating"
  git submodule update --init --recursive
  got=$(git -C oai rev-parse HEAD)
fi
[ "$got" = "$expected" ] || fail "submodule still wrong: $got vs $expected"
[ -f oai/pf_dl_pipe_logger.lua ]   || fail "oai/pf_dl_pipe_logger.lua missing"
[ -f oai/dl_metrics_forwarder.py ] || fail "oai/dl_metrics_forwarder.py missing"
echo "submodule OK ($got)"

H "Namespace + SCC"
if ocr get project "$NS" >/dev/null 2>&1; then
  echo "namespace $NS already exists (re-using)"
else
  ocr new-project "$NS" >/dev/null
fi
ocr create sa oai-privileged -n "$NS" >/dev/null 2>&1 || true
ocr adm policy add-scc-to-user privileged -z oai-privileged -n "$NS" >/dev/null

H "Docker Hub pull secret"
if ocr get secret docker-hub -n "$NS" >/dev/null 2>&1; then
  echo "docker-hub secret already exists in $NS"
else
  [ -n "${DOCKERHUB_USER:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ] \
    || fail "docker-hub secret missing AND DOCKERHUB_USER/DOCKERHUB_TOKEN unset"
  ocr create secret docker-registry docker-hub \
    --docker-server=docker.io \
    --docker-username="$DOCKERHUB_USER" \
    --docker-password="$DOCKERHUB_TOKEN" \
    -n "$NS" >/dev/null
fi
for sa in default oai-privileged builder; do
  ocr secrets link "$sa" docker-hub --for=pull -n "$NS" >/dev/null
done

H "Generate ad-hoc overlays for $NS"
OVR_AGENT="$REPO/deploy/k8s/overlays/.tmp-$NS"
OVR_OAI="$REPO/deploy/k8s/oai/overlays/.tmp-$NS"
rm -rf "$OVR_AGENT" "$OVR_OAI"
mkdir -p "$OVR_AGENT" "$OVR_OAI"

cat > "$OVR_AGENT/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NS
resources:
  - ../../base
patches:
  - path: configmap-patch.yaml
  - path: secret-patch.yaml
  - path: storageclass-patches.yaml
images:
  - name: multi-agent-commag
    newName: image-registry.openshift-image-registry.svc:5000/$NS/multi-agent-commag
    newTag: latest
EOF
cat > "$OVR_AGENT/configmap-patch.yaml" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: agents-config
data:
  GNB_API_URL: "http://oai-gnb:8000"
EOF
cat > "$OVR_AGENT/secret-patch.yaml" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: agents-secrets
type: Opaque
stringData:
  INFLUXDB_TOKEN: "agentran-dev-token"
EOF
cat > "$OVR_AGENT/storageclass-patches.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: influxdb
spec:
  volumeClaimTemplates:
    - metadata: {name: data}
      spec: {accessModes: [ReadWriteOnce], storageClassName: nfs-client, resources: {requests: {storage: 10Gi}}}
    - metadata: {name: config}
      spec: {accessModes: [ReadWriteOnce], storageClassName: nfs-client, resources: {requests: {storage: 1Gi}}}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ollama
spec:
  volumeClaimTemplates:
    - metadata: {name: models}
      spec: {accessModes: [ReadWriteOnce], storageClassName: nfs-client, resources: {requests: {storage: 50Gi}}}
EOF
cat > "$OVR_OAI/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: $NS
resources:
  - ../../core
patches:
  - path: mysql-storageclass-patch.yaml
EOF
cat > "$OVR_OAI/mysql-storageclass-patch.yaml" <<'EOF'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: mysql
spec:
  volumeClaimTemplates:
    - metadata: {name: data}
      spec: {accessModes: [ReadWriteOnce], storageClassName: nfs-client, resources: {requests: {storage: 5Gi}}}
EOF

H "Apply agent side"
kubectl kustomize "$OVR_AGENT" | ocr apply -n "$NS" -f - >/dev/null

H "Set up agent image build"
ocr new-build --binary --strategy=docker --name=multi-agent-commag -n "$NS" --to-docker=false 2>&1 | tail -1 || true

H "Create mysql-init ConfigMap (binary UE keys)"
ocr create configmap mysql-init --from-file=oai_db.sql=deploy/oai-cn/oai_db.sql -n "$NS" >/dev/null 2>&1 || \
  ocr create configmap mysql-init --from-file=oai_db.sql=deploy/oai-cn/oai_db.sql --dry-run=client -o yaml | ocr apply -f - >/dev/null

H "Apply OAI CN"
kubectl kustomize --load-restrictor=LoadRestrictionsNone "$OVR_OAI" | ocr apply -n "$NS" -f - >/dev/null

H "Apply RAN BuildConfigs"
ocr apply -f deploy/k8s/oai/ran/buildconfigs.yaml -n "$NS" >/dev/null

H "Trigger all four builds in parallel"
for bc in multi-agent-commag oai-gnb oai-nrue dl-traffic; do
  ocr start-build "$bc" --from-dir=. -n "$NS" 2>&1 | tail -1
done

H "Wait for builds to complete (~25min worst case)"
wait_build() {
  local bc=$1
  local deadline=$(( $(date +%s) + 2400 ))
  while :; do
    phase=$(oc get builds -n "$NS" -l buildconfig="$bc" --no-headers --sort-by=.metadata.creationTimestamp 2>/dev/null \
            | grep -v Cancelled | tail -1 | awk '{print $4}')
    case "$phase" in
      Complete) echo "[$(date +%H:%M:%S)] $bc Complete"; return 0 ;;
      Failed|Error) echo "[$(date +%H:%M:%S)] $bc $phase"; return 1 ;;
    esac
    [ "$(date +%s)" -gt "$deadline" ] && { echo "[$(date +%H:%M:%S)] $bc timeout"; return 1; }
    sleep 60
  done
}
fail_count=0
for bc in multi-agent-commag oai-gnb oai-nrue dl-traffic; do
  wait_build "$bc" || fail_count=$((fail_count+1))
done
[ "$fail_count" -eq 0 ] || fail "$fail_count build(s) failed"

H "Apply RAN manifests"
kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s/oai/ran \
  | ocr apply -n "$NS" -f - >/dev/null

H "Wait for all 9 pods Ready"
deadline=$(( $(date +%s) + 900 ))
while :; do
  ready=$(oc get pods -n "$NS" --no-headers 2>/dev/null \
          | grep -vE 'build|ollama-init|Completed|Terminating' \
          | awk '{split($2,a,"/"); if(a[1]==a[2] && $3=="Running") c++} END{print c+0}')
  echo "[$(date +%H:%M:%S)] $ready/9 pods Ready"
  [ "$ready" -ge 9 ] && break
  [ "$(date +%s)" -gt "$deadline" ] && fail "convergence timeout — $ready/9"
  sleep 30
done

H "Verify NGAP attach (gNB Connected in AMF)"
for i in $(seq 1 30); do
  if oc logs deploy/oai-amf -n "$NS" --tail=200 2>/dev/null \
     | grep -aqE 'gnb-rfsim.*Connected|Connected.*gnb-rfsim'; then
    echo "gNB attached"
    break
  fi
  sleep 10
done

H "Intent tests"
run_intent() {
  local name="$1" intent="$2"
  local outf="$INTENTS_DIR/$name.log"
  echo ""
  echo "--- intent: $name ---"
  echo "$intent" > "$INTENTS_DIR/$name.intent"
  ocr patch configmap agents-config -n "$NS" -p "{\"data\":{\"INTENT\":\"$intent\"}}" >/dev/null
  ocr rollout restart deployment/agents -n "$NS" >/dev/null
  until [[ "$(oc get pods -l app=agents -n "$NS" --no-headers 2>/dev/null | awk 'END{print $2}')" == "2/2" ]]; do
    sleep 5
  done
  echo "agents 2/2 — tailing scheduler for 120s into $outf"
  # Tail in the background. --line-buffered so grep flushes per line
  # instead of waiting for a full block to accumulate.
  (timeout 120 oc logs -l app=agents -c scheduler-agent -n "$NS" -f 2>/dev/null \
     | grep -a --line-buffered -E 'Updating scheduler|policy:|No scheduler changes|Reasoning' \
     > "$outf") &
  local tail_pid=$!
  # L2 sub-intent: read from the scheduler-agent container (still
  # running) since the shared-state volume is mounted there too.
  # The l2-manager container is short-lived and may already be in
  # Completed phase by the time we exec.
  sleep 30
  echo "" >> "$outf"
  echo "--- L2 sub-intent ---" >> "$outf"
  ocr exec deploy/agents -c scheduler-agent -- python3 -c \
    "import json; print(json.load(open('/app/shared-state/subintents.json'))['scheduler'])" \
    >> "$outf" 2>&1 || echo "(L2 exec failed)" >> "$outf"
  wait "$tail_pid" 2>/dev/null || true
  echo ">>> last 8 lines of $outf:"
  tail -n 8 "$outf"
}
run_intent constant-5mbps   "Set both eMBB and MTC throughput limits to a constant 5 Mbps. Do not change them over time."
run_intent alternating-1-3  "Alternate the throughput limit for all UE classes between 1 Mbps and 3 Mbps every decision cycle. When the current limit is 1 set it to 3. When it is 3 set it to 1."
run_intent asymmetric       "Alternate between two states every decision cycle. State A: eMBB 10 Mbps, MTC 1 Mbps. State B: eMBB 1 Mbps, MTC 10 Mbps."

H "Final snapshot"
oc get pods -n "$NS" 2>&1 | grep -vE 'Completed|Terminating' || true
echo ""
oc get builds -n "$NS" --no-headers 2>&1 | awk '{print $1, $4}'

H "[DONE] full log: $LOG  intent logs: $INTENTS_DIR/"
