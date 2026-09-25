# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `isolation`：隔离方案，登记受影响管线（`lines`）和阀门（`valves`，含正常位置与隔离位置）。
- `gas_test`：气体检测记录，含结果、检测时间和有效期（分钟）。

## 变更施工流程

`draft → assessed → approved → in_progress → implemented → restored → commissioned`，任意施工阶段可 `rollback`。

- 开工（`start_work`）前必须：登记隔离方案；操作员、安全员分别确认（任一方 `withdraw` 撤回即清除双方确认、回到待隔离）；气体检测合格且在有效期内；无未完成的关联行动项。
- 施工中（`in_progress`）若检测过期/不合格或行动项未完成，`finish_work` 被阻塞并提示停止施工；可 `suspend` 停工、`resume` 复工（复工重新校验开工条件）。
- 恢复供料（`restore`）前必须：完工后重新气体检测合格；隔离方案上逐阀核对回正常位置（`restore_valve`，全部核对后隔离方案转为已恢复）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/entities/<id>/blockers`：变更当前阻塞项（隔离确认、气体检测、行动项、阀门核对），`must_stop`为真时应立即停止施工。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
