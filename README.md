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

## 批次切换与放行

- `switch_in`以原子事务完成交接：新批次在`switched_at`时刻成为唯一`active`批次；旧批次进入终态`switched_out`，记录`replaced_by_lot_id`与`switched_at`。审计同时写入新批次`switch_in`与旧批次`switch_out`两条记录。
- 切换时刻之后的质控运行只能使用新批次；切换前的运行仍归旧批次。旧批次在切换后放行切换前的患者结果仍按旧批次判定。
- 仪器`calibrate`必须提供新的`certificate_id`，校准链保存在`calibration_history`；按患者结果`run_at`确定当时有效的校准证书。
- 患者结果`release`按检测项目、仪器和运行时间核对：质控批次在运行时未过期且未被切出、该批次在该仪器上无未结案失控（`rejected`/`investigated`/`retesting`）、当时证书未过期且至今未换证。任一不满足时批次保持原状态，返回`ReleaseBlocked`（HTTP 409）并写入`release_blocked`审计，原因逐条列明。
- 放行成功后，批次数据和审计记录都保留所用质控批次（`release_qc_lot_id/release_qc_lot_no`）与校准证书（`release_certificate_id`）。

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
