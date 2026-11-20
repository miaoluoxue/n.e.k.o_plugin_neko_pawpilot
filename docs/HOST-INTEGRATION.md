# 宿主接入与权限清单

`neko_pawpilot` 读取欧卡 2 遥测、识屏和陪聊，并可经用户确认后接力驾驶。
当前清单只声明面板 `state:read`、`action:call`。

> 红线结论：不写 N.E.K.O 本体记忆库、对话数据库或角色记忆卡。本插件产生的
> 猫娘可见台词和隐藏上下文只通过官方 `push_message` 注入。

## 高风险权限使用矩阵（发布前必读）

本表按当前代码事实描述，**不把“需要用户确认”写成“默认关闭”**。

| 高风险能力 | 使用插件 | 为什么必须 | 当前默认状态 | 用户确认与限制 | 回滚 |
|---|---|---|---|---|---|
| 宿主记忆/对话写入 | 无 | 插件记忆可独立存储 | 未使用 | 只写插件数据目录和 Store，不写宿主记忆库 | 无需回滚 |
| 猫娘回复/隐藏上下文 | `neko_arcade`、`neko_fishpower`、`neko_pawpilot`、`neko_pbz`、`neko_terraria` | 回复必须由宿主当前人设生成 | 按功能需要调用 | 只走官方 `push_message`；不写角色记忆卡 | 开启 dry-run、关闭插件或关闭推送 |
| 代理 DLL / UE4SS | `neko_pbz` 使用代理 DLL/UE4SS；`neko_pawpilot` 使用独立遥测 DLL | PBZ 读取游戏状态；欧卡需要 SCS 遥测 | **遥测 DLL 非默认关闭**：`Runtime.start()` 会检测游戏目录并自动安装/覆盖；代码默认 `dry_run=true`，但随包 `ui_settings.json` 当前为 `false` 并会覆盖默认 | 安装遥测 DLL 当前没有独立确认，旧 DLL 也无自动备份；键鼠接管仍需提议后 `pilot_accept` | 关闭游戏，删除本插件写入的 `scs-telemetry.dll`；被覆盖的旧文件需用户备份恢复 |
| 键鼠输入注入 | 仅 `neko_pawpilot` | 用户明确同意后接力转向、油门和刹车 | **不能标为默认关闭**：打包配置会关闭 dry-run；真正接管仍需用户同意 | `dry_run` 下绝不注入；玩家抢控、超速、低油、偏离、无路点或市区自动交还；错误配置应改为安全值 | UI 急停/收回驾驶权，设置 `dry_run=true` 或停用插件 |
| 启动游戏进程 | 仅 `neko_terraria` | 拉起用户主动连接的 AI 游戏客户端 | `neko_pawpilot` 未使用 | 猫爪副驾不启动游戏进程 | 不适用 |
| 屏幕截图 | `neko_pbz`、`neko_fishpower`、`neko_pawpilot`、`neko_terraria` | 游戏视觉侦察或 HUD OCR | 自动识屏在目标游戏前台时运行；手动拍照按用户操作运行 | 只读目标游戏客户区，不持续录屏、不上传第三方 | 关闭识屏或停用插件 |
| 私有照片/文件读写 | `neko_arcade`、`neko_pawpilot` 保存照片；六个插件均有各自的数据目录 | 相册、地图缓存、设置和日志必须持久化 | 仅用户拍照或运行功能时写入 | 照片只进插件 `data_path("photos")`，不扫描私人相册，不写宿主记忆库 | 删除插件 `photos`/数据目录 |

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

当前 `Runtime.start()` 会自动调用安装器，因此**不能宣称该 DLL 默认关闭或必须
先由用户确认**。目标文件版本不同时会被直接覆盖，且安装器还没有自动备份。
后续版本应把自动安装改为显式授权动作，并在覆盖前备份原文件。当前回滚时必须
删除本插件安装的文件；如果安装前已有原文件，应由用户备份恢复。

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

