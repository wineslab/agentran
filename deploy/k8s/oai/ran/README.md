# OAI RAN on Kubernetes

gNB and nrUE (plus a `dl-traffic` sidecar) in RFsim mode, built from
the agentRAN OAI fork in this repo's `oai/` submodule.

## Layout

```
gnb-deployment.yaml      gNB Pod (SYS_NICE, initContainer templates
                         the AMF Service IP + Pod IP into gnb.yaml)
gnb-service.yaml         exposes :4043 (rfsim), :8000 (UL control),
                         :38412 (NGAP/SCTP), :2152 (GTP-U/UDP)
nrue-deployment.yaml     nrUE + dl-traffic sidecar in one Pod
                         (shared netns for dl-traffic to see
                         oaitun_ue1)
buildconfigs.yaml        ImageStreams + BinaryBuild configs for the
                         three RAN images
kustomization.yaml       configMapGenerator pulls gnb.yaml and
                         nrue.yaml from deploy/oai-ran/ — single
                         source of truth with docker-compose
```

## Deploy

```
oc new-project <ns>

# 0. Bind the privileged SCC (UPF, gNB, nrUE need elevated capabilities).
oc adm policy add-scc-to-user privileged \
  -z oai-privileged -n <ns>

# 1. Set up BinaryBuilds for the three RAN images.
oc apply -f deploy/k8s/oai/ran/buildconfigs.yaml -n <ns>

# 2. Trigger builds (each uploads the repo root as build context;
#    the OAI submodule must be checked out — see "before you start").
#    gNB and nrUE compile from source and take ~15–25 min each.
oc start-build oai-gnb    --from-dir=. -n <ns>
oc start-build oai-nrue   --from-dir=. -n <ns>
oc start-build dl-traffic --from-dir=. -n <ns>

# 3. Apply the RAN manifests (with --load-restrictor because gnb.yaml
#    and nrue.yaml are sourced from deploy/oai-ran/).
kubectl kustomize --load-restrictor=LoadRestrictionsNone \
  deploy/k8s/oai/ran | oc apply -f - -n <ns>

# 4. Point the agents at the in-cluster gNB.
oc patch configmap agents-config -n <ns> \
  -p '{"data":{"GNB_API_URL":"http://oai-gnb:8000"}}'
oc rollout restart deployment/agents -n <ns>
```

## Before you start

The image builds need the OAI submodule checked out:

```
git submodule update --init --recursive
```

Without this, the build context is missing `oai/` and Dockerfile.gnb
will fail at the `COPY oai/ ...` stage.

## How config templating works

`deploy/oai-ran/gnb.yaml` has compose-era literal IPs:
`amf_ip_address: 10.71.0.132` and
`GNB_IPV4_ADDRESS_FOR_NG_AMF: 10.71.0.140`. On k8s, an initContainer
in the gNB Pod:

1. Resolves `oai-amf` via DNS (Service ClusterIP).
2. Reads `$POD_IP` via the downward API.
3. `sed`-substitutes both into a writable copy under `/etc/oai/`,
   which the main container mounts at `/opt/oai-gnb/etc/`.

This keeps the file under `deploy/oai-ran/` unchanged, so
`docker compose up` and `kubectl apply` both work from the same
source.

nrUE has the same pattern but for the gNB Service IP (RFsim server
address), passed via `--rfsimulator.[0].serveraddr` on the command
line — no file substitution needed.

## Cluster requirements

- KubeVirt device plugin exposing `devices.kubevirt.io/tun` for the
  nrUE Pod (it needs `/dev/net/tun` for the user-plane tunnel).
- SCTP enabled on the CNI for NGAP between gNB and AMF. OVN-Kubernetes
  supports SCTP natively.
- Privileged SCC bindable to the `oai-privileged` ServiceAccount.

## Known follow-ups

- `ext-dn` (iptables MASQUERADE / iperf3 server) not yet ported —
  the nrUE Pod's `dl-traffic` sidecar just idles after the tunnel
  comes up. Add an `ext-dn` Deployment + a route through `oaitun_ue1`
  in the sidecar args.
- `oc image mirror` the upstream OAI CN images into the namespace's
  registry so rollouts don't depend on Docker Hub rate limits.
