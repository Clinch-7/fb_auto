# Facebook 自动化 API 文档

## 1. 平台中心 (auth-jump.ops-center.net)

### 登录
- **URL**: `POST https://ga-client.ops-center.net/api/user/login/`
- **Headers**:
  ```
  Content-Type: application/json;charset=UTF-8
  x-auth-vcode: <TOTP验证码>
  ```
- **Body**:
  ```json
  {
    "username": "Payton",
    "password": "PXYabc7432.."
  }
  ```
- **响应**:
  ```json
  {
    "status": 0,
    "data": {
      "id": 517,
      "username": "Payton",
      "token": "<token>",
      "callback_token": "<callback_token>",
      "organization": "Quest",
      "projects": ["X2", "V2", "X5", "X3", "V1", "X6"],
      "group": [
        {
          "name": "综合",
          "platform": [
            {"name": "app管理后台", "login_url": "https://vest-v2.tgcgame2026.com"},
            {"name": "zeus后台", "login_url": "https://zeus.ops-center.net/"}
          ]
        }
      ]
    }
  }
  ```

### 获取用户信息
- **URL**: `GET https://ga-client.ops-center.net/api/user/info/`
- **Headers**:
  ```
  x-auth-token: <token>
  x-auth-user: <user_id>
  ```

---

## 2. App管理后台 (vest-v2.tgcgame2026.com)

状态：需要单独登录，未找到登录接口

---

## 3. Facebook 开发者后台 (developers.facebook.com)

状态：需要单独 OAuth 登录

---

## 待确认

1. vest-v2.tgcgame2026.com 的登录接口是什么？
2. zeus 后台的登录接口是什么？
