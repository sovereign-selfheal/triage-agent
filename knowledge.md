# Application Knowledge Base — quarkus-buggy-app

## Overview

**Application name:** `quarkus-buggy-app`  
**Namespace:** `agentic-triage`  
**Technology:** Quarkus 3.8 (Java), RESTEasy Reactive, Micrometer + Prometheus metrics, SmallRye Health  
**Purpose:** A deliberately broken demo application used to showcase AI-driven troubleshooting. It intentionally injects failures to simulate real-world incidents.

---

## Deployment Topology

| Resource | Details |
|----------|---------|
| Deployment | `quarkus-buggy-app` in namespace `agentic-triage` |
| Service | `quarkus-buggy-app` port 8080 (HTTP) |
| Route | `quarkus-buggy-app-agentic-triage.<appsDomain>` |
| Replicas | 1 |
| Container port | 8080 |
| Health endpoints | `/q/health/live` (liveness), `/q/health/ready` (readiness) |
| Metrics endpoint | `/q/metrics` (Prometheus format) |
| Image | `quay.io/sovereign-selfheal/quarkus-buggy-app` (pinned by digest, see gitops `components/quarkus-buggy-app`) |

---

## REST Endpoints

### GET /api/products
- **Purpose:** Returns a list of products
- **Intentional fault:** ~30% of requests throw a `NullPointerException` internally, causing an HTTP 500 response
- **Normal response:** HTTP 200 with JSON array of products
- **Error signature in logs:** `java.lang.NullPointerException` in `ProductResource.getProducts()`
- **Expected Prometheus metric:** `http_server_requests_seconds_count{uri="/api/products", status="500"}` rising steadily

### GET /api/orders
- **Purpose:** Returns a list of orders
- **Intentional fault:** ~20% of requests sleep for 3 seconds before responding, simulating a slow downstream dependency
- **Normal response:** HTTP 200 within ~50ms
- **Error signature:** No errors in logs, but high p99 latency visible in Prometheus
- **Expected Prometheus metric:** `http_server_requests_seconds_sum{uri="/api/orders"}` / `_count` showing high average latency (~600ms+ vs ~50ms normal)

### GET /api/inventory
- **Purpose:** Returns current inventory levels
- **Intentional fault:** ~40% of requests return HTTP 503 Service Unavailable, simulating a downstream inventory service being down
- **Normal response:** HTTP 200 with JSON inventory data
- **Error signature in logs:** No exception — intentional `Response.status(503).build()` return
- **Expected Prometheus metric:** `http_server_requests_seconds_count{uri="/api/inventory", status="503"}` rising steadily

---

## Traffic Generator

The application includes a built-in `TrafficGenerator` that runs every 5 seconds (after a 10-second startup delay) and calls all three endpoints:
- `http://localhost:8080/api/products`
- `http://localhost:8080/api/orders`
- `http://localhost:8080/api/inventory`

This means errors will appear in metrics **even without external traffic**. A pod that is `Running` and healthy will still show a non-zero error rate.

---

## Key Prometheus Queries

| What to check | PromQL |
|---------------|--------|
| HTTP 5xx rate (all endpoints) | `rate(http_server_requests_seconds_count{namespace="agentic-triage",outcome="SERVER_ERROR"}[5m])` |
| HTTP 500 rate on /api/products | `rate(http_server_requests_seconds_count{namespace="agentic-triage",uri="/api/products",status="500"}[5m])` |
| HTTP 503 rate on /api/inventory | `rate(http_server_requests_seconds_count{namespace="agentic-triage",uri="/api/inventory",status="503"}[5m])` |
| Avg latency on /api/orders | `rate(http_server_requests_seconds_sum{namespace="agentic-triage",uri="/api/orders"}[5m]) / rate(http_server_requests_seconds_count{namespace="agentic-triage",uri="/api/orders"}[5m])` |
| Avg latency all endpoints | `rate(http_server_requests_seconds_sum{namespace="agentic-triage"}[5m]) / rate(http_server_requests_seconds_count{namespace="agentic-triage"}[5m])` |
| Request rate (all endpoints) | `rate(http_server_requests_seconds_count{namespace="agentic-triage"}[5m])` |
| Pod restarts | `kube_pod_container_status_restarts_total{namespace="agentic-triage"}` |
| Pod ready | `kube_pod_status_ready{namespace="agentic-triage"}` |

---

## Normal vs. Abnormal Baselines

| Metric | Normal (expected) | Abnormal (investigate) |
|--------|-------------------|------------------------|
| HTTP 500 rate `/api/products` | ~0.06/s (30% of ~0.2 req/s from traffic gen) | > 0.15/s or sudden spike |
| HTTP 503 rate `/api/inventory` | ~0.08/s (40% of ~0.2 req/s) | > 0.2/s or sudden spike |
| Avg latency `/api/orders` | ~600ms (20% of requests at 3s pull up the average) | > 1s or affecting all requests |
| Pod restarts | 0 | Any restart is abnormal |
| Pod status | 1/1 Running | CrashLoopBackOff, OOMKilled, Pending |
| JVM heap usage | < 256Mi | > 512Mi (OOMKill risk) |

---

## Known Failure Modes

### 1. NullPointerException on /api/products (EXPECTED)
- **Root cause:** `ProductResource.getProducts()` dereferences a null object ~30% of the time
- **Symptom:** HTTP 500, stacktrace in pod logs, `SERVER_ERROR` in Prometheus
- **Fix (for demo):** This is intentional — no fix needed. Recommend adding null check in `ProductResource.java`

### 2. Service Unavailable on /api/inventory (EXPECTED)
- **Root cause:** `InventoryResource.getInventory()` returns 503 ~40% of the time
- **Symptom:** HTTP 503, no stacktrace (intentional response), `SERVER_ERROR` in Prometheus
- **Fix (for demo):** This is intentional — simulates a flaky downstream service

### 3. High Latency on /api/orders (EXPECTED)
- **Root cause:** `OrderResource.getOrders()` calls `Thread.sleep(3000)` ~20% of the time
- **Symptom:** p99 latency > 3s, no errors in Prometheus outcome, requests eventually succeed
- **Fix (for demo):** This is intentional — simulates a slow database or downstream timeout

### 4. Pod CrashLoopBackOff (UNEXPECTED)
- **Root cause:** OOMKill (JVM heap too large), missing image, bad env config
- **Symptom:** `kubectl get pods` shows `CrashLoopBackOff`, event `OOMKilled` or `Error`
- **Fix:** Check `oc describe pod`, look for OOMKill or image pull errors, check resource limits

### 5. No metrics in Prometheus (UNEXPECTED)
- **Root cause:** ServiceMonitor not applied, wrong label selector, or pod not running
- **Symptom:** All PromQL queries return empty results
- **Fix:** Check `oc get servicemonitor -n agentic-triage`, verify labels match the Service

---

## SLOs (Demo Targets)

| Endpoint | Target availability | Current (intentional) |
|----------|--------------------|-----------------------|
| /api/products | 99% | ~70% (30% 500s) |
| /api/inventory | 99% | ~60% (40% 503s) |
| /api/orders | 99% p99 < 500ms | ~80% within 500ms (20% at 3s) |

---

## Troubleshooting Checklist

When asked to diagnose `quarkus-buggy-app`, always:

1. **Check pod phase** — `Running` with 0 restarts is the baseline; any other state is a problem
2. **Check Prometheus for error rates** — use the queries above; compare against the "normal" baseline
3. **Identify which endpoints are affected** — products (NPE), inventory (503), orders (latency) have distinct patterns
4. **Retrieve pod logs** — look for `NullPointerException` stacktraces; absence of errors with 503s is also diagnostic
5. **Distinguish intentional vs. unintentional faults** — the three known faults above are expected; anything else (OOMKill, CrashLoop, ImagePull errors) is a real incident
6. **Quantify impact** — always report error rates as percentages and latency as p50/p99 values from Prometheus
