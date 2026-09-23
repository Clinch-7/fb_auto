# Facebook 自动化工具使用说明



## 1. 工具目标

工具用于自动完成以下流程：

1. 从 Vest 获取品牌应用信息。
2. 使用 Roxy 浏览器中已登录的 Facebook 账号创建 Meta App。
3. 写入 Facebook Basic Settings、Android 包名、Activity、Key Hash。
4. 发布 Facebook App，并接受 AMM 协议。
5. 在 Adjust 中关联 Facebook 合作伙伴，并写入事件映射。
6. 回写 Vest 中的 Facebook App ID。
7. 更新飞书监控表中的品牌名、App ID、App Secret。
8. 支持通过飞书/Lark 私聊或群聊命令触发配置和广告授权。

核心规则：

- 所有系统都使用命令中的完整品牌名，例如 `qstand231`、`abcd231`、`abc123`。
- 工具不会从品牌名中提取数字，也不会自动补前缀。
- Roxy Profile 由本机工具界面选择，飞书命令中通常不需要附带 Profile ID。

## 2. 交付文件

需要两个文件放在同一个目录：

- `Facebook_Auto_Tool.exe`
- `app_config.json`

首次启动时，如果目录中没有 `app_config.json`，程序会自动生成默认模板。建议先退出程序，补全配置后再重新启动。

运行过程中会自动生成：

- `runtime/`：运行数据、任务队列、Cookie、日志等。
- `runtime/repair_tasks.json`：下架重建任务队列。

不要把不同团队的 `app_config.json` 混用。

## 3. 前置条件

需要准备：

1. Windows 环境。
2. Roxy 浏览器已安装并正常运行。
3. Roxy API 可访问，默认地址通常是 `http://127.0.0.1:50000`。
4. Roxy Profile 中已经登录 Facebook，并能访问 Meta Developers。
5. Vest 账号配置可用。
6. Adjust API Token 可用。
7. Lark/飞书机器人已完成权限配置，并已加入需要监听的私聊或群聊。
8. 飞书监控表已创建，并授权给机器人访问。

## 4. app_config.json 配置说明

配置文件必须放在 exe 同目录。

示例结构：

```json
{
  "roxy": {
    "api_host": "http://127.0.0.1:50000",
    "api_key": "",
    "workspace_id": null
  },
  "vest": {
    "ops_login_url": "https://ga-client.ops-center.net/api/user/login/",
    "vest_login_url": "https://api-vest.tgcgame.com/admin/login_by_token/",
    "vest_api_base": "https://api-vest.tgcgame2026.com",
    "ops_username": "填写 Vest/OPS 用户名",
    "ops_password": "填写 Vest/OPS 密码",
    "ops_totp_secret": "填写 2FA/TOTP Secret"
  },
  "lark": {
    "openapi_base": "https://open.larksuite.com",
    "app_id": "填写 Lark App ID",
    "app_secret": "填写 Lark App Secret",
    "bot_name": ""
  },
  "monitor_sheet": {
    "openapi_base": "",
    "app_id": "",
    "app_secret": "",
    "spreadsheet_token": "填写飞书表格 token",
    "sheet_id": "填写工作表 sheet_id"
  },
  "adjust": {
    "api_token": "填写 Adjust API Token",
    "api_base": "https://api.adjust.com/dashboard/api",
    "enabled": true,
    "facebook_event_mappings": {
      "Register": "fb_mobile_complete_registration",
      "Small": "fb_mobile_purchase",
      "Medium": "fb_mobile_purchase",
      "Large": "fb_mobile_purchase"
    }
  }
}
```

字段说明：

- `roxy.api_host`：Roxy 本地 API 地址。
- `roxy.api_key`：Roxy API Key，没有启用鉴权可留空。
- `roxy.workspace_id`：指定 Roxy 工作区 ID；为空时会读取所有可访问工作区。
- `vest.*`：用于登录 Vest 并读取品牌包名、Key Hash、备注链接等信息。
- `lark.app_id/app_secret`：用于启动飞书长连接监听和发送消息。
- `monitor_sheet.spreadsheet_token`：飞书表格 URL 中 `/sheets/` 后面的 token。
- `monitor_sheet.sheet_id`：具体工作表 ID。
- `adjust.api_token`：Adjust Dashboard API Token。
- `adjust.facebook_event_mappings`：Adjust 事件和 Facebook 合作伙伴事件映射。


## 5. 飞书监控表格式

监控表至少需要前三列：

| 列 | 字段 | 说明 |
| --- | --- | --- |
| A | 品牌名 | 完整品牌名，例如 `qstand231` |
| B | Facebook App ID | 工具创建成功后写入 |
| C | Facebook App Secret | 工具创建成功后写入 |

工具同步表格时按 A 列完整品牌名查找：

- 如果已存在该品牌，更新 B/C 列。
- 如果不存在，新建一行。

注意：表格中使用命令里的完整品牌名；如果命令是 `abc123`，表格也写 `abc123`。

## 6. GUI 使用方式

启动 `Facebook_Auto_Tool.exe` 后：

1. 确认 `Roxy地址` 正确。
2. 如 Roxy 需要 API Key，填写 `API Key`。
3. 程序会读取 Roxy Profile，并尝试打开 Profile 访问 Facebook 检测登录状态。
4. 在账号下拉框中选择要执行自动化的 Profile。
5. 点击 `启动 Lark 监听`。
6. 飞书中发送命令即可触发任务。

## 7. 飞书命令

### 7.1 查看可用 Profile

私聊或群聊发送：

```text
选择Profile
```

机器人会回复当前工具读取到的 Roxy Profile 列表，并标记当前选择的 Profile。

### 7.2 配置品牌

推荐发送完整品牌名：

```text
配置qstand231
```

也支持其他前缀：

```text
配置abcd231
```

执行规则：

- Facebook 创建应用名使用完整品牌名。
- Vest 查询使用完整品牌名。
- Adjust 搜索使用完整品牌名。
- 飞书监控表写入完整品牌名。

如果发送：

```text
配置231
```

则所有系统都会使用 `231`。如果发送 `abc123`，所有系统都会使用 `abc123`。

### 7.3 广告授权

格式：

```text
授权 qstand274,abcd275
1714489949761975
2103942320189234
```

说明：

- 第一行 `授权` 后面填写一个或多个 App 目标，多个目标用英文逗号或中文逗号分隔。
- 目标可以是完整品牌名，也可以是 Facebook App ID。
- 后续每行填写广告账号 ID。
- 工具会先读取当前 App 已授权广告账号，重复 ID 不会再次添加。
- 只会保存本次新增的广告账号。

也可以写成单行：

```text
授权 qstand274 1714489949761975 2103942320189234
```

## 8. Lark/飞书机器人权限要求

机器人至少需要：

- 能接收消息事件：`im.message.receive_v1`
- 能发送文本消息
- 能访问指定飞书表格
- 已加入目标私聊或群聊

如果群聊中没有反应：

1. 确认机器人已加入该群。
2. 确认发送的是当前配置的机器人，不是另一个旧机器人。
3. 群聊里建议 @ 机器人后再发送命令。
4. 确认 Lark 开放平台事件订阅已启用长连接模式。
5. 确认 GUI 日志出现 `Lark 监听启动` 和 `ready event_key=im.message.receive_v1`。

## 9. Roxy 使用注意事项

工具不会保存 Facebook 密码。

Facebook 登录状态来自 Roxy Profile：

- 外部团队需要先在 Roxy Profile 中手动登录 Facebook。
- Profile 中有 Facebook Cookie 后，工具才能复用登录态。
- 手动登录下账号并选择记住密码。



## 10. Adjust 自动配置

创建 Facebook App 成功后，工具会自动执行 Adjust Facebook 合作伙伴配置：

1. 使用完整品牌名搜索 Adjust App。
2. 如果未关联 Facebook partner，则新增关联。
3. 如果已关联，则更新最新 Facebook App ID。
4. 确保以下事件存在：
   - `Register`
   - `Small`
   - `Medium`
   - `Large`
5. 写入默认 Facebook 事件映射：
   - `Register -> fb_mobile_complete_registration`
   - `Small -> fb_mobile_purchase`
   - `Medium -> fb_mobile_purchase`
   - `Large -> fb_mobile_purchase`

如需修改事件映射，改 `app_config.json` 中的 `adjust.facebook_event_mappings`。

## 11. 失败处理规则

如果配置失败：

- 工具会尝试删除本次新创建的 Facebook App。
- 删除优先走接口，接口删除后仍可见时会 fallback 到 UI 删除。
- 飞书会收到配置失败通知，包含：
  - 品牌名
  - Adjust 品牌名
  - 失败原因
  - 新建 App ID
  - 清理结果

常见失败：

- `This app package cannot be added due to existing policy violations.`  
  包名被 Meta 判定有政策问题，无法继续配置。需要更换包名或处理 Meta 侧限制。

- `Invalid Privacy Policy URL`  
  隐私政策 URL 被 Meta 判定无效。需要确认 Vest 返回的备注/政策链接可公开访问。

- `Meta is not logged in`  
  Roxy Profile 未登录 Facebook，或 Cookie 失效。

- `Create app requires login/checkpoint confirmation`  
  Meta 需要人工确认、密码或风控验证。人工完成后点击 `确认继续`。

- `No Adjust app found for brand xxx`  
  Adjust 中没有搜索到完整品牌名对应的 App。确认 Adjust 品牌名和命令品牌名一致。

## 12. 队列说明

工具本机会维护任务队列：

```text
runtime/repair_tasks.json
```

正常飞书命令会创建任务并立即执行。若重复发送同一品牌命令，工具会避免重复创建正在执行的任务。


## 13. 推荐操作流程

外部团队首次使用：

1. 把 `Facebook_Auto_Tool.exe` 和 `app_config.json` 放在同一目录。
2. 填好 `app_config.json`。
3. 打开 Roxy，确认目标 Profile 已登录 Facebook。
4. 启动工具。
6. 选择正确 Profile。
7. 点击 `启动 Lark 监听`。
8. 在飞书发送 `选择Profile` 测试机器人是否有响应。
9. 发送 `配置qstand231` 执行真实配置。
10. 成功后检查：
    - 飞书通知
    - Meta App 是否发布
    - Adjust Facebook partner 是否更新
    - Vest 是否写入 App ID
    - 飞书监控表是否新增/更新完整品牌名

## 14. 交付和安全注意事项

- 不要把一个团队的 `app_config.json` 发给其他团队。
- 不要在群聊、文档或截图中泄露：
  - Vest 密码
  - TOTP Secret
  - Lark App Secret
  - Adjust API Token
  - Roxy API Key
- Roxy Profile 中的 Facebook 登录态由外部团队自行维护。
- 如果更换电脑，需要重新配置 Roxy、登录 Facebook，并复制/新建对应的 `app_config.json`。
