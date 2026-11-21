# 物理实验室只读接入

统一调用路径：前端 / Agent → LaboratoryService → 设备适配器 → MCP。

- `models.py`：来源、设备、通道、能力和快照。测量单位统一为 V、A、Ah、Wh；缺失值为 null。
- `service.py`：每个来源独立缓存 5 秒，合并并发刷新；查询超时 20 秒，断线保留上次数据并标记过期。
- `adapters/`：新威、蓝电的查询与响应转换；只调用设备实际支持的能力。
- `adapters/__init__.py`：注册来源。兼容现有循环仪协议的设备可继承 CyclerAdapter；其他协议实现 source()、read() 即可。

HTTP：`GET /api/laboratory/sources`、`GET /api/laboratory/sources/{source_id}/snapshot`。原 `/bts`、`/land` 地址保留兼容。
Agent：`laboratory_sources`、`laboratory_snapshot`，在实验规划和实验阶段可用。前端与 Agent 使用同一套模型、服务逻辑，各进程分别缓存。

能力由 MCP 实际提供的工具和 health 响应共同确定。蓝电目前仅发现通道，查询服务在线不代表仪器或通道在线。records、protocols 尚未统一接入，标记为 false；既有厂商 MCP 工具仍保留兼容。

本模块不包含启动、停止、下发工步等控制操作。自动化扣电保留工作站入口，不虚构循环仪通道。
