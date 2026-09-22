# DICOM 批量匿名化 API

基于 **FastAPI + pydicom + Docker** 的 DICOM 批量匿名化服务。上传一个包含 DICOM
影像的 ZIP，服务会：

1. 以用户提供的**稳定密钥（secret）**经 HMAC-SHA256 派生**患者级伪名**
   （同一患者 + 同一密钥永远得到相同伪名，便于跨批次纵向关联但无法回溯身份）；
2. 重写 **Patient / Study / Series / SOP / Frame of Reference** 的 UID；
3. **递归遍历所有序列（SQ）项**，同步重写 `ReferencedStudySequence`、
   `ReferencedSOPSequence`、`SourceImageSequence` 等引用，保持
   Patient→Study→Series→Instance 的检查关系不变；
4. 清除 / 置空身份字段（DICOM Basic Application Confidentiality Profile 标签集 +
   全部 PN 人名、机构、医生、申请单号、描述、私有标签等），对日期做**患者级
   稳定整体平移**以保留时间间隔；
5. 返回**逐标签变更预览（preview）**与**匿名化 ZIP 下载**；
6. 遇到非法 DICOM、坏 ZIP、引用缺失等情况不中断整批，而是返回**逐文件诊断**
   （error / warning + 标签路径 + 原因）。

> 像素数据保持原样。对含烧录身份信息（Burned In Annotation）的影像，本服务不
> 做像素擦除，请在入组前自行确认来源。

---

## 一键启动

需要本机已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build -d
```

启动后服务监听：

| 项目 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| Swagger 文档（可在线试发请求） | <http://localhost:8000/docs> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| 健康检查 | <http://localhost:8000/healthz> |

停止：`docker compose down`

端口通过 `docker-compose.yml` 的 `ports: "8000:8000"` 映射，改左侧即可换宿主端口。

---

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/jobs` | multipart 上传 ZIP + 参数，同步处理并返回 `jobId` 与变更预览 |
| `GET`  | `/api/jobs/{jobId}` | 任务状态与 error/warning 计数 |
| `GET`  | `/api/jobs/{jobId}/preview` | 完整标签变更预览（上传响应内已含同一份） |
| `GET`  | `/api/jobs/{jobId}/download` | 下载匿名化 ZIP |
| `DELETE` | `/api/jobs/{jobId}` | 立即清除任务与产物 |
| `GET`  | `/healthz` | 存活探测 |

任务产物仅存于内存，**默认 1 小时后自动清除**（服务重启同样清空）。

### `POST /api/jobs` 的表单参数

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 必填 | DICOM 影像 ZIP（≤ 3 GB） |
| `secret` | string | 必填 | 用户稳定密钥，**至少 8 字符**。决定伪名与 UID 的确定性派生 |
| `uid_root` | string | 空 | 可选组织 UID 根（点分十进制 OID，如 `1.2.840.12345`）；留空使用合法的 `2.25.<UUIDv5 整数>` UID |
| `shift_dates` | bool | `true` | 患者级稳定日期平移（0–180 天，向过去），保留时间间隔 |
| `keep_patient_sex` | bool | `true` | 保留 `PatientSex`（非直接身份字段） |
| `remove_private_tags` | bool | `true` | 递归删除全部私有数据元素 |
| `remove_curves` | bool | `true` | 删除 50xx 老式 Curve 数据 |
| `remove_overlays` | bool | `false` | 删除 60xx Overlay（可能含烧录图形） |
| `strict_references` | bool | `false` | 为 `true` 时，引用目标缺失按错误处理并以 422 拒绝任务 |
| `include_report` | bool | `true` | 在输出 ZIP 中附 `ANONYMIZATION_REPORT.json` |

---

## 使用示例

### cURL

```bash
# 1) 上传并拿到预览
curl -s -X POST http://localhost:8000/api/jobs \
  -F "file=@studies.zip;type=application/zip" \
  -F "secret=my-stable-secret-2026" \
  -F "uid_root=" \
  -F "shift_dates=true" | tee preview.json

# 从返回 JSON 中取 jobId
JOB_ID=$(python3 -c "import json;print(json.load(open('preview.json'))['jobId'])")

# 2) 下载匿名化结果
curl -OJ "http://localhost:8000/api/jobs/${JOB_ID}/download"
```

### Python（requests）

```python
import requests

with open("studies.zip", "rb") as fh:
    resp = requests.post(
        "http://localhost:8000/api/jobs",
        files={"file": ("studies.zip", fh, "application/zip")},
        data={"secret": "my-stable-secret-2026", "shift_dates": "true"},
        timeout=3600,
    )
resp.raise_for_status()
body = resp.json()
job_id = body["jobId"]
preview = body["preview"]

for f in preview["files"]:
    print(f["path"], "->", f["patient"], f["studyInstanceUID"])
    for d in f["diagnostics"]:
        print("  ", d["severity"], d["code"], d["message"], d.get("element"))

zip_bytes = requests.get(
    f"http://localhost:8000/api/jobs/{job_id}/download", timeout=3600
).content
open("studies_anon.zip", "wb").write(zip_bytes)
```

---

## 预览（preview）结构

```jsonc
{
  "jobId": "...",
  "status": "ready",
  "options": { "shiftDates": true, "...": "..." },
  "totals": {
    "members": 4, "dicomFiles": 3, "nonDicomFiles": 1,
    "patients": 1, "errorCount": 1, "warningCount": 1,
    "outputBytes": 123456
  },
  "patients": [
    { "pseudonymID": "ANON-VK2JC8N936FB",
      "pseudonymName": "ANON^VK2JC8N936FB",
      "dateOffsetDays": -134, "fileCount": 2 }
  ],
  "files": [
    {
      "path": "study/IM002",
      "patient": "ANON-VK2JC8N936FB",
      "studyInstanceUID": "2.25.3282...",
      "seriesInstanceUID": "2.25.7710...",
      "sopInstanceUID": "2.25.9981...",
      "changes": [
        { "tag": "StudyInstanceUID", "action": "uid",
          "old": "1.2.840.11....", "new": "2.25.3282..." },
        { "tag": "PatientID", "action": "set",
          "old": "P-001", "new": "ANON-VK2JC8N936FB" },
        { "tag": "ReferencedSOPSequence[0].ReferencedSOPInstanceUID",
          "action": "uid", "old": "1.2.3...", "new": "2.25.9981..." },
        { "tag": "StudyDate", "action": "set",
          "old": "20240102", "new": "20230821" }
      ],
      "diagnostics": [
        { "severity": "warning", "code": "missing_reference",
          "message": "引用的 UID 在本批次内未找到对应实例...",
          "tag": "ReferencedSOPInstanceUID",
          "element": "ReferencedSOPSequence[1].ReferencedSOPInstanceUID" }
      ]
    }
  ],
  "nonDicomFiles": ["readme.txt"],
  "uidMapsSample": { "study": {"...": "..."}, "series": {}, "sop": {} },
  "diagnostics": [ /* 批次级 + 逐文件诊断，含 path 字段 */ ],
  "truncated": { "fileChanges": false, "uidMaps": false }
}
```

`action` 含义：`set`（重写）、`blank`（置空）、`uid`（UID 映射）、`delete`（删除元素/序列）。
预览默认包含前 50 个文件的完整变更清单与每类前 200 条 UID 映射（完整诊断始终返回，
不截断）；ZIP 内的 `ANONYMIZATION_REPORT.json` 始终包含全量内容。

---

## 诊断与错误约定

| 场景 | HTTP | 行为 |
| --- | --- | --- |
| 成功（可含 warning） | `201` | 返回 `jobId` 与预览，文件可下载 |
| ZIP 损坏 / 加密 / 超体积 | `400 / 413` | 拒绝并说明原因 |
| `secret` 太短、`uid_root` 非法 | `422` | 参数校验错误 |
| ZIP 中**没有任何**合法 DICOM | `422` | 响应体 `detail.diagnostics` 内含逐文件原因 |
| `strict_references=true` 且存在缺失引用 | `422` | 不创建任务，返回全部缺失引用诊断 |
| 任务不存在 / 已过期 | `404` | — |

逐文件诊断 `code` 包括：

- `not_dicom`（error）：无法解析为 DICOM（坏文件 / 非 DICOM）；
- `not_dicom_full_read`（error）：元信息可读但完整读取失败；
- `missing_reference`（warning / error）：序列内引用的 UID 在本批次中没有对应
  声明实例。**该引用仍会被确定性重写**，避免原始 UID 泄露；
- `unsafe_path`（error，批次级）：ZIP 内含绝对路径 / `..` 穿越成员，已跳过；
- `study_patient_mismatch`（warning）：同一 Study UID 关联了不同患者标识；
- `duplicate_member_name`（warning）：ZIP 内重名成员；
- `duplicate_sop_uid`（warning）：匿名化后 SOP UID 碰撞（HMAC 下概率极低的防护）。

非法 DICOM 文件**不会**进入输出 ZIP，但会出现在预览与
`ANONYMIZATION_REPORT.json` 的诊断中。

---

## 匿名化策略说明

### UID 重写

- 重写顶层 `SOPInstanceUID` / `StudyInstanceUID` / `SeriesInstanceUID` /
  `FrameOfReferenceUID` / `ConcatenationUID`，以及文件头
  `MediaStorageSOPInstanceUID`；
- 递归扫描所有 SQ，重写其中每一个实例型 UI 元素（多值 UI 同样处理）；
- **不**改写 `SOPClassUID`、`TransferSyntaxUID`、`ReferencedSOPClassUID`、
  `CodingSchemeUID` 等“类型型” UID；
- 默认输出 `2.25.<uuid5(secret-derived namespace, original UID).int>`，符合
  DICOM PS3.5 对 `2.25` UUID UID 的要求；提供 `uid_root` 时改为
  `root.<HMAC-SHA256 派生数字>`，总长 ≤ 64；
- 映射对整个批次一致：所有引用与被引用实例经同一函数映射，关系天然闭合；
  对批次外的悬空引用也同样映射（不泄露原值）并报告。

### 患者级伪名

- 分组键优先取 `PatientID`，缺失时取 `PatientName`，再缺失归入同一
  “无标识”组；
- 伪名：`PatientID = ANON-<12 位 base32 HMAC 令牌>`，
  `PatientName = ANON^<同一令牌>`；
- `StudyID` 按研究派生为独立 8 位令牌。

### 日期

- 启用 `shift_dates` 时，给每位患者分配 0–180 天的稳定负向偏移（由 secret +
  患者键派生），**所有 DA 标签（含序列内、生日）整体平移**，保留纵向时间间隔。

### 标签

- 所有 PN（人名，含序列内）按保密配置删除；身份类 LO/SH/CS 等置空；
- 删除 `OriginalAttributesSequence`、`OtherPatientIDsSequence` 等身份序列；
- 默认递归删除私有标签；写入：
  - `PatientIdentityRemoved = YES`
  - `DeidentificationMethod = DICOM BATCH ANONYMIZER`
  - `DeidentificationMethodCodeSequence`（DCM 113100）
  - 日期平移时 `LongitudinalTemporalInformationModified = MODIFIED`

---

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

随仓库提供一个基于真实 pydicom 的端到端测试（构造无像素数据集，验证 UID 引用
闭合、伪名稳定性、诊断分类等）：

```bash
pip install pytest
python -m pytest tests/ -q
# 或直接运行：
python tests/test_anonymizer.py
```

## 安全注意

- `secret` 是相关性的唯一钥匙，请妥善保管；更换 `secret` 即产生一套互不相关的
  新伪名，无法与历史批次关联。
- 本服务无内置鉴权，暴露到非可信网络时请放在网关 / 反向代理之后并增加认证、TLS。
- 任务数据仅存内存并带 TTL；请勿用于不可接受数据短暂落驻留内存的合规场景。
