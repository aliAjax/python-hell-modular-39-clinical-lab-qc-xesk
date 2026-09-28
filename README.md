# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。

## 质控批次切换与放行约束

- `qc_lot.switch_in`（新批次动作）以**单一事务**完成接管：新批次置为`active`并记录`replaces_lot_id`、`switched_at`/`activated_at`；旧批次同时置为`retired`并记录`replaced_by_lot_id`、`retired_at`。切换时刻之后只能在新批次上登记质控运行，切换前的质控运行及其患者结果仍按旧批次判定放行。
- 旧批次存在未结案失控（`rejected`、`investigated`、`retesting`）不阻止切换，但失控单保持开放、可追溯。
- `instrument.calibrate`把新证书（`certificate_id`、`calibration_due`、`calibrated_at`）追加到`calibration_history`证书时间线，不再覆盖旧记录。
- `result_batch.release`按检测项目、仪器和运行时间逐项确认：
  - 质控运行已接受且项目、仪器与结果批次一致，检测项目仍启用；
  - 所用质控批次在结果运行时刻有效：未过期，且运行时间落在该批次的接管/停用窗口内；
  - 同一批次在该仪器上没有未结案失控；
  - 仪器处于`ready`，质控运行时刻与结果运行时刻持有的是**同一张有效校准证书**（换证后必须用新证书重测质控）。
- 任一条件不满足时放行被拒绝（HTTP 409），结果批次保持`waiting`原状态，错误信息说明具体原因（批次过期、证书换证、未结案失控等）。
- 放行审计记录包含`qc_lot_id`、`lot_no`、批次接管/停用/过期时间、质控与结果运行时间以及两个时刻的`certificate_id`；批次切换为旧批次额外写入一条`retire`审计，记录接替批次与切换时间。
