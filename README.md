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
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/claim`，进入评估后按当前版本领单，必须提交`expected_version`
- `POST /api/items/{id}/transfer`，当前领办人发起交接，必须提交`to_actor`和`reason`
- `POST /api/transfers/{id}/confirm`，接收人确认后更换主责
- `POST /api/transfers/{id}/reject`，接收人驳回，原领办人继续负责
- `GET /api/audit`

允许角色：operator, compliance_officer, director, viewer。按浓度与许可限值计算超标倍数，异常读数先进入评估；关闭前必须没有未完成整改项。

## 领办与交接

事件进入`assessing`后，合规员（compliance_officer）从共享队列中按事件当前版本领单：

- 同一事件只保留一名领办人，重复领单和过期`expected_version`都会返回409。
- 评估阶段只有领办人可以提交处置意见（records），未领单或非领办人会被拒绝。
- 领办人发起转交必须填写原因和接收人；接收人确认前主责不变，确认后领办人更换且版本递增，驳回则原领办人继续，可再次发起转交。
- 只有指定接收人可以确认或驳回，交接只能处理一次。
- 列表和详情返回`owner`（领办人）、`pending_transfer`（待确认交接）和`version`。
- 领办、转交、确认、驳回均写入审计链（动作：claim/transfer/confirm/reject）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
