---
name: correct
description: 同类工程错误再次出现或用户要求为重复问题增加护栏时，按根因把纠错写成架构、类型、静态检查或测试。
---

# 升级重复错误

1. 从当前错误和可访问的历史记录确认至少两个同根因事例，引用对应问题、日志或提交。
2. 读 `docs/RULES.md` 的规则 ID 和执行者。优先移除错误路径，再依次选择类型约束、静态检查或行为回归测试。
3. 用历史事例证明新检查失败，再修复并重跑同一入口。按项目实际规则台账执行检查：Python 项目运行 `python .engineering-kit/python-style`，TypeScript 项目运行 `npm run fitness`。涉及行为时运行项目测试；TypeScript 应用可运行 `npm run verify:app` 取得浏览器证据。
4. 在规则台账记录执行者、历史事例、验收命令和必要的限期例外。
