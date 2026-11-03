# ADR-0002 Codex 适配：codex exec 子进程，不用 Python SDK

## 状态
已定案（2026-11-03）

## 决策
V1.0 的 Codex 引擎绑定本地 codex CLI（0.166.0）子进程，读其 JSON 事件流。
不引入 openai-codex Python SDK。

## 理由
- 本地 `codex` CLI 继承 `C:\Users\uida0712\.codex\config.toml` 的
  mimo2codex-agnes 本地网关（127.0.0.1:8788/v1，模型 agnes-3.0-flash），零额外配置即可跑通，
  是 V1.0 当前唯一可验证的链路。
- openai-codex SDK 本机未安装，其对自定义 provider/base_url 的支持未经证实。

## 后果
- Codex 适配依赖本机 codex CLI 与配置共存；CLI 缺席应报清晰错误。
- 日后 SDK 对本地网关的支持证实后可升级；切换局限在 adapters/codex.py 一个文件，
  adapter 接缝保证引擎可替换。