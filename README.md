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
- `isolation`：施工隔离单，登记受影响管线与阀门，并串起隔离、气体检测、开工与恢复供料。

## 施工隔离与恢复流程

变更批准后、实施前，必须按隔离单完成以下闭环（防止邻线阀门漏关、口头确认开工）：

1. **登记隔离单**：列出受影响管线与阀门（必须包含邻线阀门），初始状态为`待隔离`。
2. **双重确认隔离**：操作员与安全员分别确认；任一方`withdraw`撤回，立即回到`待隔离`，既往气体检测作废。
3. **气体检测**：双方确认后提交合格检测（`passed=true`与未来的`valid_until`），检测不合格或有效期已过不能登记。
4. **开工门禁**：双方确认 + 检测合格且未过期 + 关联行动项全部`verified`，任一不满足都会返回具体阻塞项并禁止`start_work`。
5. **施工中**：检测过期或行动项重新打开，页面与`GET /api/entities/<id>/blockers`显示具体阻塞项，必须`stop_work`；停工后检测记录清空，复工需重新检测。
6. **恢复供料**：必须重新气体检测（`retest`）并对登记阀门逐个核对（`valve_checks`，缺阀、错阀、重复核对都会拒绝），之后才能`restore_feed`。
7. 隔离单到达`已恢复供料`后，变更才允许`implement`。

隔离单动作：`confirm_operator`、`confirm_safety`、`withdraw`、`gas_test`、`start_work`、`stop_work`、`retest`、`restore_feed`；其中操作员/安全员确认与撤回受角色限制。演示页面（打开根路径）可直接操作全流程并显示阻塞项。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；支持`units/changes/action_items/isolations`别名。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/<id>/blockers`：读取该对象当前具体阻塞项（隔离单/变更施工门禁）。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
