# Facebook Auto Hybrid

独立新版工具。策略是接口优先，浏览器负责登录、风控验证和提供 fresh token；接口拿不到关键结果时才回退到页面操作。

## PyCharm 本地调试

直接用 PyCharm 打开并运行：

```text
gui_run.py
```

页面里填写：

- 完整品牌名
- Roxy API 地址（默认 `http://127.0.0.1:50000`）
- Roxy API Key（如本机 Roxy 开启了 API Key 校验就填写）
- Facebook 账号（从 Roxy Profile 自动读取）
- 浏览器（默认 Roxy）
- 是否发布 App
- 是否更新飞书监控表

如果浏览器里需要人工登录、密码验证或风控验证，处理完页面后点击 GUI 里的“确认继续”。

## Roxy 浏览器接入

新版主线不再自己启动 Chrome/Edge 保存 cookie，而是调用 Roxy 本地 API 启动指定 Profile，再通过 CDP 接管浏览器：

1. 先打开 Roxy，并确认本地 API 可用。
2. 在 Roxy 中创建/登录 Facebook Profile。
3. 在 GUI 中填写 Roxy API 地址/API Key。
4. 点击“刷新账号”，选择对应 Profile。
5. 点击“检查登录”或“开始执行”。

Roxy 配置会保存到：

- `runtime/roxy_config.json`

## 流程

1. 登录 Vest，按完整品牌名读取包名、Key Hash、备注 URL。
2. 打开 Meta 开发者后台并复用当前账号 cookie。
3. 创建 Meta App。
4. 保存 Basic Settings。
5. 配置 Android Package、Activity、Key Hash，其中 Activity 从 Vest 返回字段读取。
6. 发布 App。
7. 回写 Vest 的 FB App ID。
8. 按命令中的完整品牌名更新飞书监控表；如果旧行已有 secret，会保留旧 secret。

## 运行数据

运行目录会自动创建：

- `runtime/browser_profiles/...`
- `runtime/fb_cookies_*.json`
- `runtime/request_logs/*.json`

`gui_run.py` 是 PyQt 页面；`debug_run.py` 仍保留给临时单步调试。

## Lark 命令监听

给同事使用时，推荐只打开 GUI：

1. 可选填写本机 `Worker` 名称，例如 `tom`。
2. 填写 Roxy API 地址/API Key。
3. 刷新并选择本机 Roxy Profile。
4. 点击“启动 Lark 监听”。

飞书命令格式：

```text
配置237
配置qstand237
```

Profile 由工具里当前选择的 Roxy Profile 决定。查看当前机器可用 Profile：

```text
选择Profile
```

如果多台电脑同时运行，也可以指定 Worker 名称；只有 Worker 名称匹配的电脑会执行：

```text
配置237 tom
```

也支持临时指定 profile：

```text
配置237 tom <RoxyProfileDirId>
```

后台脚本入口仍保留：

```text
python lark_command_listener.py --worker-name tom --roxy-profile-id <RoxyProfileDirId>
```

## 下架监控联动

`facebook_app_monitor.py` 确认某个 App 为真实异常后，会写入重建任务：

- `runtime/repair_tasks.json`

GUI 中点击“处理下架任务”会读取一个 `pending` 任务，使用当前选择的 Roxy Profile 执行完整创建/配置/发布/回写流程。任务状态包括：

- `pending`
- `running`
- `success`
- `failed`

命令行调试：

```text
python task_worker.py list
python task_worker.py run-one --roxy-profile-id <RoxyProfileDirId>
```
