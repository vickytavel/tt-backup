# tavern-mcp

给写面具用的 MCP server。只读卡片、世界书、预设；写独立面具文件。

规格见 [`../tavern-mcp-spec.md`](../tavern-mcp-spec.md)。

## 运行

无额外依赖，需要能 import `tavern-bot/tavernlib`。

```bash
TAVERN_LIBRARY=/opt/tavern-library python3 tavern-mcp/server.py
```

环境变量：

| 变量 | 作用 |
|---|---|
| `TAVERN_LIBRARY` | 资源库根目录，与 bot 共用 |
| `TAVERN_MASKS` | 面具目录，默认 `$TAVERN_LIBRARY/masks` |
| `TAVERN_MCP_READONLY=1` | 关闭 `mask_save` |

## 客户端配置

```json
{
  "mcpServers": {
    "tavern": {
      "command": "python3",
      "args": ["/opt/tavern-bot/../tavern-mcp/server.py"],
      "env": { "TAVERN_LIBRARY": "/opt/tavern-library" }
    }
  }
}
```

远端部署时把 `command` 换成 `ssh`：

```json
{
  "mcpServers": {
    "tavern": {
      "command": "ssh",
      "args": ["vps", "TAVERN_LIBRARY=/opt/tavern-library python3 /opt/tavern-mcp/server.py"]
    }
  }
}
```

这样没有 MCP 直连的客户端也能通过 SSH 用上；但凭据会落在客户端配置里，能直连就别绕。

## 工具

读：`card_index` `card_field` `card_list` `worldbook_index` `worldbook_get`
`worldbook_match` `preset_blocks` `preset_block` `mask_list` `mask_get` `mask_validate`
写：`mask_save`（默认 `dry_run=true`）

## 典型流程

1. `card_index` 看有哪些料
2. `worldbook_match` 用面具草稿匹配 8 条候选
3. `worldbook_get` 只取要用的几条正文
4. `card_field` 抓 `description` / `first_mes` 的语感
5. `preset_block(main)` 确认输出约束
6. `mask_save` 先 `dry_run` 看 diff，再 `dry_run=false` + `expected_revision`

## 测试

```bash
python3 tavern-mcp/test_server.py
```
