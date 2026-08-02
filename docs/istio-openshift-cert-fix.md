# Istio Certificate Rotation Fix on OpenShift

## The Problem

On OpenShift clusters with upstream Istio installed alongside OpenShift's built-in gateway controller (`istiod-openshift-gateway`), Istio gateway pods experience repeated x509 certificate errors every ~30 minutes:

```
DeltaAggregatedResources gRPC config stream to xds-grpc closed:
  connection error: transport: authentication handshake failed:
  tls: failed to verify certificate: x509: certificate signed by unknown authority
```

This causes:
- Gateway pods losing XDS connection to istiod for 5-30+ minutes
- HTTP 503 errors on all traffic routed through the gateway
- EPP (Endpoint Picker) unable to route requests to serving pods
- Benchmark tests failing or hanging

## Root Cause

Two istiod instances run on the cluster, each with a **different CA**:

1. **Upstream istiod** (`istio-system/istiod`) — installed via `istioctl install`
2. **OpenShift istiod** (`openshift-ingress/istiod-openshift-gateway`) — managed by OpenShift's Ingress Operator

Both distribute `istio-ca-root-cert` ConfigMaps to ALL namespaces. They overwrite each other's root certs every ~30 minutes. When a gateway pod trusts CA-A (from one istiod) but connects to istiod using CA-B (from the other), the TLS handshake fails.

## How to Diagnose

### 1. Check for disconnects on gateway pods

```bash
for gw in $(kubectl get pods -n $NAMESPACE --no-headers | grep gateway | awk '{print $1}'); do
  count=$(kubectl logs $gw -n $NAMESPACE | grep "closed since" | wc -l)
  echo "$gw: $count disconnects"
done
```

### 2. Compare CAs between the two istiods

```bash
# Upstream istiod CA
kubectl get secret istio-ca-secret -n istio-system \
  -o jsonpath='{.data.ca-cert\.pem}' | base64 -d | \
  openssl x509 -noout -startdate

# OpenShift istiod CA
kubectl get secret istio-ca-secret -n openshift-ingress \
  -o jsonpath='{.data.ca-cert\.pem}' | base64 -d | \
  openssl x509 -noout -startdate
```

If the dates differ → they have different CAs → root cause confirmed.

### 3. Check root cert in your namespace

```bash
kubectl get configmap istio-ca-root-cert -n $NAMESPACE \
  -o jsonpath='{.data.root-cert\.pem}' | \
  openssl x509 -noout -startdate
```

This should match the CA of the istiod your gateways connect to (upstream in `istio-system`).

### 4. Check who last modified the root cert ConfigMap

```bash
kubectl get configmap istio-ca-root-cert -n $NAMESPACE \
  -o jsonpath='{.metadata.managedFields}' | python3 -c "
import sys, json
for f in json.load(sys.stdin):
    print(f'Manager: {f.get(\"manager\")}, time: {f.get(\"time\")}')"
```

### 5. Check istiod push activity

```bash
# High push rate indicates cert churn
kubectl logs -l app=istiod -n istio-system --since=5m | \
  grep "Push debounce" | wc -l
```

Normal: <10 per 5 minutes. Problem: >50 per 5 minutes.

## The Fix

### Step 1: Scope the OpenShift istiod to its own namespace

Prevent the OpenShift istiod from distributing root certs to all namespaces:

```bash
# Add rootNamespace to OpenShift istiod config
kubectl get cm istio-openshift-gateway -n openshift-ingress -o yaml | \
  sed 's/accessLogFile: \/dev\/stdout/rootNamespace: openshift-ingress\naccessLogFile: \/dev\/stdout/' | \
  kubectl apply -f -

# Restart to pick up the change
kubectl delete pod -l app=istiod -n openshift-ingress
```

Verify:
```bash
kubectl get cm istio-openshift-gateway -n openshift-ingress \
  -o jsonpath='{.data.mesh}' | grep rootNamespace
# Should show: rootNamespace: openshift-ingress
```

### Step 2: Synchronize CAs

Copy the OpenShift istiod's CA to the upstream istiod so both use the same trust chain:

```bash
# Export OpenShift CA
kubectl get secret istio-ca-secret -n openshift-ingress -o json | \
  python3 -c "
import sys, json
d = json.load(sys.stdin)
d['metadata'] = {'name': 'istio-ca-secret', 'namespace': 'istio-system'}
for k in ['resourceVersion', 'uid', 'creationTimestamp']:
    d['metadata'].pop(k, None)
    d.pop(k, None)
print(json.dumps(d))
" | kubectl apply -f -

# Restart upstream istiod to use the new CA
kubectl delete pod -l app=istiod -n istio-system
```

### Step 3: Refresh root certs in your namespace

```bash
# Delete stale root cert — istiod will recreate with the correct CA
kubectl delete configmap istio-ca-root-cert -n $NAMESPACE

# Wait for recreation
sleep 10

# Verify all three match
echo "Our istiod CA:"
kubectl get secret istio-ca-secret -n istio-system \
  -o jsonpath='{.data.ca-cert\.pem}' | base64 -d | \
  openssl x509 -noout -startdate

echo "OpenShift istiod CA:"
kubectl get secret istio-ca-secret -n openshift-ingress \
  -o jsonpath='{.data.ca-cert\.pem}' | base64 -d | \
  openssl x509 -noout -startdate

echo "Root cert in namespace:"
kubectl get configmap istio-ca-root-cert -n $NAMESPACE \
  -o jsonpath='{.data.root-cert\.pem}' | \
  openssl x509 -noout -startdate
```

All three should show the same date.

### Step 4: Restart gateway and EPP pods

```bash
# Delete all gateway and EPP pods in the namespace
for pod in $(kubectl get pods -n $NAMESPACE --no-headers | \
  grep -E "gateway|gaie" | awk '{print $1}'); do
  kubectl delete pod $pod -n $NAMESPACE &
done
wait
```

### Step 5: Verify

```bash
# Wait 60 seconds for pods to stabilize
sleep 60

# Check — should show 0 disconnects, 0 restarts
for gw in $(kubectl get pods -n $NAMESPACE --no-headers | \
  grep gateway | awk '{print $1}'); do
  count=$(kubectl logs $gw -n $NAMESPACE | grep "closed since" | wc -l)
  restarts=$(kubectl get pod $gw -n $NAMESPACE \
    -o jsonpath='{.status.containerStatuses[0].restartCount}')
  echo "$gw: $count disconnects, $restarts restarts"
done
```

Wait 30-60 minutes and re-check to confirm no new disconnects occur during the cert rotation window.

## Additional Issues Found

### KServe Self-Signed Certificate Storm

KServe `LLMInferenceService` CRDs with unhealthy pods (e.g., `ContainerStatusUnknown`) can trigger the KServe controller to regenerate self-signed certs every ~16 seconds. Each regeneration triggers an istiod config push to ALL Envoy proxies, destabilizing XDS connections cluster-wide.

**Diagnose:**
```bash
kubectl logs -l app=istiod -n istio-system --since=5m | \
  grep "kserve-self-signed-certs" | wc -l
```

**Fix:** Delete the unhealthy `LLMInferenceService` resources:
```bash
kubectl delete llminferenceservice --all -n $AFFECTED_NAMESPACE
```

### Duplicate cert-manager Operator Subscriptions

Two OLM subscriptions for the cert-manager operator (one in `cert-manager-operator`, one in `openshift-cert-manager-operator`) can cause cert conflicts.

**Diagnose:**
```bash
kubectl get subscription -A --no-headers | grep cert-manager
```

If you see two subscriptions, remove the one that's not actively running:
```bash
kubectl delete subscription $OLD_SUBSCRIPTION -n $OLD_NAMESPACE
kubectl delete csv $OLD_CSV -n $OLD_NAMESPACE
```

## Environment

- OpenShift 4.19+
- Upstream Istio 1.29.x installed via `istioctl`
- OpenShift Ingress Operator managing `istiod-openshift-gateway`
- Gateway API Inference Extension enabled
