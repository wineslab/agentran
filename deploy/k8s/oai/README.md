# OAI on Kubernetes

Kustomize manifests for the OAI 5G stack from `deploy/docker-compose.yml`,
deployable alongside the agents in `deploy/k8s/`. Two trees:

```
deploy/k8s/oai/core/    OAI CN: mysql, AMF, SMF, UPF
deploy/k8s/oai/ran/     OAI RAN: gNB, nrUE + dl-traffic sidecar (RFsim)
```

The CN is upstream OAI images (`oaisoftwarealliance/oai-{amf,smf,upf}`)
plus a stock `mysql:8.0`. The RAN is built from the agentRAN OAI fork
in `oai/` (this repo's submodule) via an OpenShift BinaryBuild — see
`deploy/k8s/oai/ran/README.md`.

## Deploy from scratch

### One-shot driver

The full path — namespace, SCC, Docker Hub pull-secret, agent build, CN
apply, RAN BuildConfigs + builds, RAN apply, NGAP attach check, and
three agentic-loop intent tests — is captured in `../openshift-deploy.sh`:

```
NS=my-namespace \
DOCKERHUB_USER=… DOCKERHUB_TOKEN=… \
deploy/k8s/openshift-deploy.sh
```

Idempotent and resumable; re-running against the same `NS` re-uses
the namespace, secret, and BuildConfigs. Outputs at
`/tmp/<NS>-deploy.log` and `/tmp/<NS>-intents/`.

### Manual

```
oc new-project <ns>

# 1. Bind the privileged SCC (UPF needs NET_ADMIN+SYS_ADMIN, gNB/UE
#    need NET_ADMIN+NET_RAW+SYS_NICE, agent-side does not).
oc adm policy add-scc-to-user privileged \
  -z oai-privileged -n <ns>

# 2. oai_db.sql contains binary UE keys that kustomize can't embed as
#    a YAML string. Create the ConfigMap directly.
oc create configmap mysql-init \
  --from-file=oai_db.sql=deploy/oai-cn/oai_db.sql -n <ns>

# 3. Apply the CN. --load-restrictor=LoadRestrictionsNone is needed
#    because mini_nonrf_config.yaml is sourced from deploy/oai-cn/
#    (single source of truth with docker-compose).
kubectl kustomize --load-restrictor=LoadRestrictionsNone \
  deploy/k8s/oai/overlays/<name> | oc apply -f - -n <ns>

# 4. Build and apply the RAN — see deploy/k8s/oai/ran/README.md.
```

## What's verified

Full 5G SA stack end-to-end on OpenShift (`multi-agent-commag-dev-v2`):

- **CN**: mysql Bound on `nfs-client`, AMF/SMF/UPF all 1/1 Running,
  SMF↔UPF PFCP heartbeats on N4.
- **RAN**: gNB attaches to AMF over NGAP/SCTP (NG SETUP completed),
  nrUE connects to gNB over RFsim, registers, and gets a PDU session
  with UE IP `12.1.1.2` from the UPF subnet. `oaitun_ue1` interface
  up inside the nrUE Pod.
- **Agents↔gNB**: scheduler agent's MCP loop drives
  `POST http://oai-gnb:8000/api/v1/scheduler-config` and gets
  `HTTP/1.1 200 OK` back — LLM-decided throughput limits applied to
  the live UE.
- All cross-component traffic via plain k8s Service DNS in one namespace.

## Networking decisions

OAI CN configs already use Service-name hostnames (`host: oai-amf`,
`host: oai-smf`, `host: oai-upf`, `host: mysql`), so the CN works
on plain k8s DNS — no Multus needed.

The RAN configs (`deploy/oai-ran/gnb.yaml`) reference the compose
static IPs `10.71.0.132` (AMF) and `10.71.0.140` (gNB). On k8s the
gNB container resolves these at startup via `envsubst` against
`AMF_IP` (from a Service ClusterIP lookup) and `POD_IP` (from the
downward API) — see `ran/gnb-deployment.yaml`.

## OpenShift specifics

- OAI AMF/SMF write `/var/run/oai_{amf,smf}00.pid` — the restricted
  SCC's random UID can't, so they run as UID 0 via the
  `oai-privileged` ServiceAccount.
- OAI UPF needs `privileged: true` + `NET_ADMIN`/`SYS_ADMIN`. Same SA.
- All OAI deployments pin `kubernetes.io/arch: amd64`. This lets the
  multiarch-tuning-operator skip its Docker Hub manifest inspection,
  which otherwise hits unauthenticated pull rate limits during
  rollouts.

## Known follow-ups

- `oc image mirror docker.io/oaisoftwarealliance/oai-{amf,smf,upf}:v2.1.10`
  into the namespace's image registry so rollouts don't depend on
  Docker Hub rate limit windows.
- Move MySQL credentials to an external secret (currently dev-defaults).
- `ext-dn` (iptables MASQUERADE / iperf3 server for UE data plane) is
  not yet ported — the CN is functional without it for control-plane
  testing.
