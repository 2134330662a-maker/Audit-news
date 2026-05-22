# 审计新闻每日推送工具

每日自动抓取国际审计行业新闻，翻译摘要后推送到飞书 & 钉钉群机器人。

## 快速开始

### 1. 推送到 GitHub

将本目录初始化为 Git 仓库并推送到 GitHub：

```bash
cd audit-news-push
git init
git add .
git commit -m "init: audit news daily push"
git branch -M main
git remote add origin git@github.com:YOUR_USER/YOUR_REPO.git
git push -u origin main
```

### 2. 配置 GitHub Secrets

在仓库 **Settings → Secrets and variables → Actions → Repository secrets** 中添加以下变量：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `FEISHU_WEBHOOK` | 至少一个 | 飞书机器人 Webhook 地址（含 token） |
| `FEISHU_SECRET` | 按需 | 飞书机器人签名校验密钥（如开启了签名校验则必填） |
| `DINGTALK_WEBHOOK` | 至少一个 | 钉钉机器人 Webhook 地址（含 access_token） |
| `DINGTALK_SECRET` | 按需 | 钉钉机器人加签密钥（如开启了加签则必填） |
| `RSSHUB_BASE_URL` | 否 | RSSHub 实例地址（默认 `https://rsshub.app`） |
| `LLM_API_KEY` | 否 | OpenAI 兼容 API Key，用于 LLM 翻译增强 |
| `LLM_API_BASE` | 否 | LLM API 地址（默认 `https://api.openai.com/v1`） |
| `LLM_MODEL` | 否 | 模型名（默认 `gpt-3.5-turbo`） |


### 3. 新闻源

| 类别 | 来源 | 类型 |
|---|---|---|
| 国际 | Journal of Accountancy | 直接 RSS |
| 国际 | IFRS Foundation | RSSHub |
| 国际 | PCAOB | RSSHub |
| 国际 | CFO.com | RSSHub |


> 新闻源可在 `main.py` 的 `NEWS_SOURCES` 列表中按需增删或禁用。

### 4. 本地测试

```bash
pip install -r requirements.txt

# 至少设置一个 Webhook
export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
export FEISHU_SECRET="your_secret"          # 可选
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export DINGTALK_SECRET="your_secret"         # 可选

python main.py
```

### 5. 安全说明

- 所有 Webhook 地址和密钥通过 GitHub Secrets 注入，**不硬编码在代码中**
- 国内新闻源仅限社会团体和专业媒体，**不抓取 .gov.cn 政府网站**
- 推送内容标注"仅供学习参考"
