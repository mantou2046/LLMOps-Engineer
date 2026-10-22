# mlflow chart —— 集群内的 tracking server

> W3 D18 产出。设计取舍见决策日志 DEC-028；这里只讲**怎么用**和**为什么这么做**。

## 这是什么

一个**独立的** Helm chart（不是 `llmops-chart` 的子模板），把 MLflow tracking server 跑在 kind 集群里：

| 组件 | 说明 |
| --- | --- |
| Deployment | 1 副本，`--workers=2`（默认 4 个在 2Gi 下必 OOM） |
| 后端 | Postgres（`default/postgres` 这个 StatefulSet 里的 `mlflow` 库，跨 ns 走 FQDN） |
| artifacts | **代理模式**：`--serve-artifacts` + `--artifacts-destination /mlartifacts`，落在一块 2Gi PVC 上 |
| Service | `mlflow:5000`（ns 内） |
| Ingress | `mlflow.llmops.local`（复用 ingress-nginx） |

客户端看到的是 `mlflow-artifacts:/`，**不持有任何存储凭据**——这是选代理模式而不是直连模式的原因。

## 前置（一次性）

**1. 镜像**：官方 `ghcr.io/mlflow/mlflow:v3.17.1` 里**没有 psycopg2**，必须自建：

```bash
cd /e/Projects/LLMOps-Engineer
docker build -t llmops-mlflow:v3.17.1-pg infra/k8s/mlflow
kind load docker-image llmops-mlflow:v3.17.1-pg --name llmops
```

**2. 数据库**：

```bash
kubectl exec -n default postgres-0 -- createdb -U llmops mlflow
```

**3. 密码**（不进 Git）：从现有 Secret 生成 `values-secret.yaml`（已被 `.gitignore` 挡住）：

```bash
kubectl get secret llmops-secrets -n default -o jsonpath='{.data.POSTGRES_PASSWORD}' \
  | base64 -d | xargs -I{} printf 'db:\n  password: "%s"\n' {} \
  > infra/k8s/mlflow/values-secret.yaml
```

**4. hosts**（管理员权限，只需一次）：

```powershell
$p = "$env:SystemRoot\System32\drivers\etc\hosts"
Copy-Item $p "$p.bak" -Force
"`n# LLMOps kind 集群`n127.0.0.1 llmops.local`n127.0.0.1 llmops-dev.local`n127.0.0.1 mlflow.llmops.local" |
  Add-Content -Path $p -Encoding ASCII
```

## 部署 / 升级 / 卸载

```bash
helm upgrade --install mlflow ./infra/k8s/mlflow -n mlflow --create-namespace \
  -f infra/k8s/mlflow/values-secret.yaml

helm uninstall mlflow -n mlflow          # PVC 不会自动删，见下
kubectl delete pvc mlflow-artifacts -n mlflow   # 真删数据才执行这句
```

## A / B 两套环境（DEC-028）

| | A · 日常 | B · 演示 |
| --- | --- | --- |
| 形态 | 本机进程 `python -m mlflow server --port 5000` | 集群内 helm release |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | `http://mlflow.llmops.local` |
| 何时用 | 跑实验、调参、看 UI | 展示「整条链路在 K8s 里可复现」 |
| 代价 | 本机得开着进程 | 得先 `kind` 起集群、加 hosts |

`.env` 默认走 A；演示时临时改 B（`.env.example` 里两行都留着）。

## 排障：五个坑（都踩过）

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `exec: "server": executable file not found` | 官方镜像 `ENTRYPOINT` 为空、`CMD=[python3]`，`args` 顶掉了 CMD | 加 `command: ["mlflow"]` |
| `ModuleNotFoundError: No module named 'psycopg2'` | 官方镜像不带 PG 驱动 | 自建镜像 `pip install psycopg2-binary` |
| `ModuleNotFoundError: No module named 'psycopg'` | SQLAlchemy 2.1 把 `postgresql://` 默认解析成 psycopg3 | URI 写 `postgresql+psycopg2://` |
| `OOMKilled`（exit 137） | 默认 4 个 gunicorn worker，每个加载整套 mlflow | `--workers=2` + limit 3Gi |
| `Invalid Host header - possible DNS rebinding attack detected` | MLflow 3.x 的 Host 白名单 | `MLFLOW_SERVER_ALLOWED_HOSTS` 加域名 |

## 验证清单

```bash
kubectl get pods,pvc -n mlflow                       # 1/1 Running，PVC Bound
curl -s -o /dev/null -w '%{http_code}\n' http://mlflow.llmops.local/health   # 200（需 hosts）
kubectl exec -n mlflow deploy/mlflow -- python -c "
import mlflow; mlflow.set_tracking_uri('http://localhost:5000')
e = mlflow.set_experiment('smoke')
with mlflow.start_run(): mlflow.log_metric('x', 1); mlflow.log_text('hi','n.txt')
print('ok', e.experiment_id)"
kubectl exec -n mlflow deploy/mlflow -- find /mlartifacts -name 'n.txt'   # 证明 PVC 可写
```
