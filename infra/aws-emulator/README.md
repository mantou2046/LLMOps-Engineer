# AWS Emulator 本地 IaC 验证

本机跑 AWS 官方本地模拟器 aws-emulator 1.0.322，用它代替真实云账号来练 IaC（Infrastructure as Code，基础设施即代码）。这是 LLMOps 作品集里补「上云」缺口的一层练习：S3（Simple Storage Service，简单存储服务）存 MLflow artifact、DynamoDB 存评估结果，全用 Terraform 建和拆。所有请求都落在 `127.0.0.1:9297`，不连真实 AWS，也不需要 AWS 账号。

目录里两份文件是同一套验证脚本，VM 与 PC 对照用：`boto3_check.py` 走 SDK（Software Development Kit，软件开发工具包）测 API 保真度，`tf/main.tf` 走 provider 测资源编排。项目层落点按仓库既有布局定：已有 `.tf` 在 `../terraform/`，故本目录建在同级父目录 `infra/` 下，与它平级、互不引用。

## 前置

Docker Desktop（本次 29.9.2，Linux 容器模式）、Python 3.13、镜像 `aws-emulator:1.0.322`。工具链装在 `D:\Software\0\aws-emulator\`：venv 里是 boto3 1.44.1 + awscli 1.46.3，`bin\terraform.exe` 是 1.17.2。

## 起模拟器

容器是常驻进程，先起它再做别的。端口映射必须写 `127.0.0.1:9297:9297`，写成 `9297:9297` 会把服务暴露到局域网。

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.aws\emulator"
docker run -d --name aws-emulator --memory=2g --memory-swap=2g `
  -p 127.0.0.1:9297:9297 `
  -e EMULATOR_TELEMETRY=off `
  -v "$env:USERPROFILE\.aws\emulator:/home/emulator/.aws/emulator" `
  aws-emulator:1.0.322 -p 9297
curl.exe -k https://127.0.0.1:9297/__admin/health
```

健康检查回 `ok` 即可。容器会在挂载目录下生成 `9297\config`、`9297\emulator_credentials`、`9297\ca.pem`。

## 设环境变量

每个新开的 PowerShell 都要重设一次。容器生成的 `config` 里 `ca_bundle` 是容器内路径，宿主机用不了，必须靠 `AWS_CA_BUNDLE` 显式指宿主机路径；不要改那份 config。

```powershell
$venv = "D:\Software\0\aws-emulator\venv"
$emu  = Join-Path $env:USERPROFILE ".aws\emulator\9297"
$env:PATH = "$venv\Scripts;" + $env:PATH
$env:AWS_CONFIG_FILE             = Join-Path $emu "config"
$env:AWS_SHARED_CREDENTIALS_FILE = "${emu}\emulator_credentials"
$env:AWS_CA_BUNDLE               = Join-Path $emu "ca.pem"
$env:AWS_EC2_METADATA_DISABLED   = "true"
```

`$env:PATH` 前置 venv 不能省。awscli v1 的 `aws.cmd` 靠 PATH 找 `python.exe`，不前置会抓到系统 Python，然后报 `ModuleNotFoundError: No module named 'awscli'`。

## 跑 boto3 验证

```powershell
cd E:\Projects\LLMOps-Engineer\infra\aws-emulator
D:\Software\0\aws-emulator\venv\Scripts\python.exe .\boto3_check.py
```

期望 9 行 PASS、结尾 `== boto3 验证：9 通过 / 0 失败 ==`、退出码 0。脚本里 DynamoDB 建表等 ACTIVE 的轮询不要删。

## 跑 Terraform 三命令

凭据从模拟器生成的文件里取，不手填假值。

```powershell
cd E:\Projects\LLMOps-Engineer\infra\aws-emulator\tf
$TF  = "D:\Software\0\aws-emulator\bin\terraform.exe"
$cred = Get-Content (Join-Path $env:USERPROFILE ".aws\emulator\9297\emulator_credentials")
$env:AWS_ACCESS_KEY_ID     = ($cred | Select-String "aws_access_key_id").Line.Split("=")[1].Trim()
$env:AWS_SECRET_ACCESS_KEY = ($cred | Select-String "aws_secret_access_key").Line.Split("=")[1].Trim()
$env:TF_IN_AUTOMATION = "1"
& $TF init
& $TF plan
& $TF apply -auto-approve
& $TF destroy -auto-approve
```

`main.tf` 里的 `insecure = true` 对应模拟器自签名证书，是既定写法。`~/.aws/` 下 emulator 子目录以外的文件一律不动。

## 本次实测（2026-11-12，Windows PC / Docker 路线）

| 项 | 结果 |
| --- | --- |
| 镜像版本 | `Version: 1.0.322` / `Release date: 2026.11.10` / `Commit: 7753983c6e495ec3612695b1336bf4936fc7b7fd` |
| 健康检查 | `ok`，9297 目录三个文件齐（另有自动生成的 `truststore.p12`） |
| CLI 往返 | `aws s3 ls` 空；建桶、上传、下载 16 字节，`fc.exe` 报「找不到差异」 |
| boto3 | 9 通过 / 0 失败，退出码 0 |
| Terraform plan 1 | `Plan: 4 to add, 0 to change, 0 to destroy` |
| Terraform apply | `Apply complete! Resources: 4 added` |
| 独立读回 | S3 回 `source=terraform fmt=1.0 hit1=0.82`；DynamoDB 回 `hit1.N = 0.82` |
| Terraform plan 2 | `Plan: 0 to add, 1 to change, 0 to destroy` |
| Terraform destroy | `Destroy complete! Resources: 4 destroyed` |
| destroy 复核 | head-bucket 报 404；describe-table 报 `ResourceNotFoundException` |

provider 由 `terraform init` 装的是 `hashicorp/aws v5.100.0`（`~> 5.0` 约束下）。

## 已知保真漂移一条

二次 plan 不是全干净，与 VM 侧结果一致：DynamoDB 表的 `on_demand_throughput` 块被模拟器回读成 `max_read_request_units = -1` / `max_write_request_units = -1`，Terraform 期望 `null`，于是报 1 处 in-place 更新。这是模拟器回读口径的缺口，不是配置写错，不要为了「plan 干净」去改 `main.tf`。

## 停容器即清场

docker stop aws-emulator 停掉容器后，模拟器会删掉它在 %USERPROFILE%\.aws\emulator\9297\ 下生成的文件（config、emulator_credentials、ca.pem），内存里的桶、表等资源也一并消失；下次启动会重新生成一套新凭据。所以：凭据值不要抄进任何脚本或笔记（每次启动都变）；每次重新启动后，先按本文的环境变量一节重设四个变量，Terraform 的凭据也要从新生成的 emulator_credentials 重新取；停机前的验证数据需要重跑 boto3_check.py 与 Terraform 流程重建。
