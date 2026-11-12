#!/usr/bin/env python3
"""S3 + DynamoDB 验证（跨平台版，VM/PC 共用）：对着 aws-emulator 端点跑 boto3。
凭据走默认链——运行前按施工单设置 AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE
指向模拟器生成的 config / emulator_credentials，并设 AWS_CA_BUNDLE。
"""
import boto3, os, sys, time

ENDPOINT = os.environ.get("AWS_EMULATOR_ENDPOINT", "https://127.0.0.1:9297")
CA = os.path.expanduser("~/.aws/emulator/9297/ca.pem")

session = boto3.session.Session()
s3 = session.client("s3", endpoint_url=ENDPOINT, verify=CA, region_name="us-east-1")
ddb = session.client("dynamodb", endpoint_url=ENDPOINT, verify=CA, region_name="us-east-1")

ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    print(("PASS" if cond else "FAIL"), name, extra)
    ok, fail = ok + bool(cond), fail + (not cond)

# 1. 建桶
try:
    s3.create_bucket(Bucket="llmops-artifacts")
except s3.exceptions.BucketAlreadyOwnedByYou:
    pass
buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
check("create_bucket+list_buckets", "llmops-artifacts" in buckets, str(buckets))

# 2. 传对象 + 读回（模拟 MLflow artifact）
payload = b"run_id=abc123 model=qa-combo v2 metrics.fmt=1.0\n"
s3.put_object(Bucket="llmops-artifacts", Key="mlflow/run-abc123/metrics.txt", Body=payload)
got = s3.get_object(Bucket="llmops-artifacts", Key="mlflow/run-abc123/metrics.txt")["Body"].read()
check("put_object+get_object 内容一致", got == payload)

# 3. 开版本控制 + 同 key 覆盖，取回版本列表
s3.put_bucket_versioning(Bucket="llmops-artifacts", VersioningConfiguration={"Status": "Enabled"})
v = s3.get_bucket_versioning(Bucket="llmops-artifacts").get("Status")
check("put/get_bucket_versioning", v == "Enabled", f"status={v}")
s3.put_object(Bucket="llmops-artifacts", Key="mlflow/run-abc123/metrics.txt", Body=b"v2\n")
vers = s3.list_object_versions(Bucket="llmops-artifacts", Prefix="mlflow/")
check("list_object_versions 有≥2版", len(vers.get("Versions", [])) >= 2, f"n={len(vers.get('Versions', []))}")

# 4. 预签名 URL 生成（只验生成）
url = s3.generate_presigned_url("get_object", Params={"Bucket": "llmops-artifacts", "Key": "mlflow/run-abc123/metrics.txt"}, ExpiresIn=600)
check("generate_presigned_url", url.startswith(ENDPOINT + "/"))

# 5. 错误保真：读不存在的 key 应回 NoSuchKey
try:
    s3.get_object(Bucket="llmops-artifacts", Key="nope.txt")
    check("NoSuchKey 错误保真", False)
except s3.exceptions.NoSuchKey:
    check("NoSuchKey 错误保真", True)
except Exception as e:
    check("NoSuchKey 错误保真", False, f"got {type(e).__name__}: {e}")

# 6. DynamoDB：建表（异步，须等 ACTIVE）/写/查 + 非法表名错误保真
try:
    ddb.create_table(TableName="eval-results",
                     AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                     BillingMode="PAY_PER_REQUEST")
except ddb.exceptions.ResourceInUseException:
    pass
status = None
for _ in range(20):
    status = ddb.describe_table(TableName="eval-results")["Table"]["TableStatus"]
    if status == "ACTIVE":
        break
    time.sleep(0.5)
check("dynamodb 表状态到 ACTIVE", status == "ACTIVE", f"status={status}")
ddb.put_item(TableName="eval-results", Item={"id": {"S": "gs-001"}, "hit1": {"N": "0.82"}})
item = ddb.get_item(TableName="eval-results", Key={"id": {"S": "gs-001"}})["Item"]
check("dynamodb create/put/get", item["hit1"]["N"] == "0.82")
try:
    ddb.create_table(TableName="ab",
                     AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                     BillingMode="PAY_PER_REQUEST")
    check("dynamodb 非法表名 ValidationException", False)
except Exception as e:
    check("dynamodb 非法表名 ValidationException", "ValidationException" in str(e))

print(f"\n== boto3 验证：{ok} 通过 / {fail} 失败 ==")
sys.exit(1 if fail else 0)
