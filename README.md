# 企业排污许可与超标处置

汇总监测和工况，判断排放超标并跟踪复测、整改、执法与复查。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8313
```

默认端口为`8313`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`（返回含`version`、`owner`、`pending_handoff`）
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/claim`，合规员按`expected_version`领单
- `POST /api/items/{id}/handoff`，领办人提交`to_actor`和`reason`发起转交
- `POST /api/handoffs/{id}/decision`，接收人提交`decision=confirm|reject`
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

## 领办与交接规则

- 事件进入评估（`assessing`及之后）才可领单；仅`compliance_officer`可领单。
- 领单必须携带`expected_version`：过期版本返回409；同一事件只保留一名领办人，重复领单返回409（数据库唯一索引在并发下兜底）。
- 转交必须填写原因和接收人；待确认期间主责仍是原领办人。只有指定接收人能处理交接：确认后更换主责，驳回则原领办继续履职。同一事件同时只允许一笔待确认交接。
- 事件已有领办人后，只有当前领办人能提交处置意见（records），其他人返回403。
- 每次领单、转交、确认、驳回均写入审计链（`claim`/`handoff`/`handoff_confirm`/`handoff_reject`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
