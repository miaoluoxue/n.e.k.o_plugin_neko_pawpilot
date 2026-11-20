# 宿主接入与权限清单

`neko_pawpilot` 读取欧卡 2 遥测、识屏和陪聊，并可经用户确认后接力驾驶。
当前清单只声明面板 `state:read`、`action:call`。

## 声明=实际

## 宿主能力

| 能力 | 实际用途 | 当前声明 |
|---|---|---|
| `@lifecycle` | 启停遥测、识屏、陪玩和安全循环 | 隐式 |
| `@plugin_entry` / `@ui.action` | 状态、自检、拍照、节奏配置、急停与智驾控制 | `action:call` |
| `push_message` | 将安全/任务/旅程事实交给宿主猫娘生成回复 | 未单列 |
| `data_path()` | UI 设置、照片、地图缓存和日志 | 未单列 |
| 独立 LLM | 可选 HTTPS provider/model/key | 未单列 |

## 高风险本机能力

### 遥测 DLL

插件可把随包 `scs-telemetry.dll` 安装到欧卡 2 的
`bin/win_x64/plugins/`。这是游戏官方插件目录中的遥测 DLL，不是代理注入 DLL。
安装前应关闭游戏，并校验文件 SHA-256。

当前安装器在目标文件不同版本时会覆盖。回滚时必须删除本插件安装的文件；
如果安装前已有原文件，应由用户备份恢复。后续实现应增加自动备份。

### 接力驾驶

仅在用户明确同意后，使用 `pydirectinput` 注入鼠标转向和键盘油门/刹车：

- 默认 `dry_run=true`；
- 检测用户抢方向盘立即让渡；
- 超速、低油、偏离车道、无路点、进入市区时自动交还；
- 不修改游戏存档或数值。

该能力可能影响真实游戏操作，必须在 UI 中持续显示接管状态并提供急停。

### 截图与 OCR

- 自动识屏只在游戏窗口位于前台时读取游戏客户区；
- 手动拍照可回退到最后记住的游戏窗口；
- 使用宿主 RapidOCR，不上传截图；
- 照片只保存在插件数据目录。

## 网络、文件与系统

- 通过 Steam 注册表和 `libraryfolders.vdf` 只读定位游戏目录；
- 本机 TCP 读取遥测；
- 写入遥测 DLL 与插件私有数据；
- 可选 HTTPS 调用独立 LLM；
- 不写 N.E.K.O 本体记忆库、对话库或角色记忆卡；猫娘发言只通过官方
  `push_message` 注入。
- 不写游戏存档，不注入代理 DLL。

## 权限差距与回滚

云玩家权限模板应覆盖：消息注入、数据目录、输入注入、游戏目录写入、屏幕捕获、
本机遥测、注册表读取和外置 HTTPS。

回滚：

- 立即执行“收回驾驶权”或急停；
- 停用插件停止遥测与控制；
- 删除游戏 `plugins` 目录中本插件安装的遥测 DLL；
- 删除插件 `photos` 与设置文件。

## 完整权限清单

权限 ID 仅用于后续映射云玩家模板，不代表当前 SDK 已声明这些字段。

| 审计 ID | 权限 | 实际 API/资源 | 降级 |
|---|---|---|---|
| `HOST_LIFECYCLE` | 启停 | `@lifecycle` | 停止遥测、识屏和控制 |
| `HOST_ENTRY_UI` | 面板入口 | `@plugin_entry`、`@ui.action` | 面板关闭后主动播报仍可存在 |
| `HOST_PUSH_RESPOND` | 事实注入并触发回复 | `push_message(ai_behavior="respond")` | dry-run/模板降级 |
| `HOST_DATA_PATH` | 数据目录 | `plugin.data_path()` | 不可写时禁用相册/设置 |
| `HOST_PERSONA_READ` | 猫娘人设 | 宿主 `config/characters.json` 等路径 | 默认人设 |
| `HOST_CTX_PRIVATE` | 目标角色解析 | `ctx._current_lanlan`、`ctx._host_ctx` | 单角色 fallback |
| `NET_LOOPBACK_TCP` | SCS 遥测 | 本机 telemetry TCP 服务 | 未连接时 NO_GAME |
| `REGISTRY_READ` | Steam 路径 | HKCU/HKLM `Software\Valve\Steam` | 未找到要求手动填目录 |
| `FS_GAME_DIR_READ` | 游戏目录探测 | `base_map.scs`、插件目录 | 无目录时禁用 |
| `FS_GAME_DIR_WRITE` | 安装遥测 DLL | `bin/win_x64/plugins/scs-telemetry.dll` | 游戏占用/权限不足时失败 |
| `NATIVE_TELEMETRY_DLL` | 原生遥测模块 | 随包 `scs-telemetry.dll` | 不安装时无遥测 |
| `SCREEN_CAPTURE` | 游戏画面识别 | GDI `BitBlt` / mss / Pillow，仅游戏客户区 | 非前台时跳过 |
| `HOST_OCR` | 画面文字识别 | 宿主共享 RapidOCR | 不可用时关闭识屏 |
| `FS_PHOTO_WRITE` | 美景相册 | 插件数据目录 `photos/` | 写失败时只返回错误 |
| `INPUT_SYNTHETIC` | 接力驾驶 | `pydirectinput` 鼠标/键盘 | 默认 dry-run；用户可随时收回 |
| `NET_HTTPS_LLM` | 独立 LLM | `httpx` | 未配置时模板/宿主 summary |

当前安装器在覆盖不同版本 DLL 前没有自动备份原文件，这是已确认的权限与安全问题。
云权限应要求 `fs:game:write`、`native:dll:install`、`input:synthetic`、
`screen:capture`、`registry:steam:read` 和本地 TCP 接收能力。

红线审计：未发现写宿主记忆库；猫娘发言只通过 `push_message`。最高风险是游戏目录
写入和键鼠注入，不是宿主记忆写入。

## 待整改

1. 遥测 DLL 覆盖前创建原文件备份；
2. 若已有不同版本 `scs-telemetry.dll`，默认停止并提示用户选择；
3. 云权限声明必须单独列出输入注入和游戏目录写入。

