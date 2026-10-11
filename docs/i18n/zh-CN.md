# duet — 两个 AI 模型协作构建，规则由程序检查

> 本页是简短的中文入门，译自英文 README。**以[英文 README](../../README.md) 为准**；每一次运行的完整记录见 [docs/QA.md](../QA.md)。

单个 AI 代理给自己的作业打分，总会说"通过了"。duet 让**第二个模型**参与进来，由程序**亲自运行你的测试**，并把每个工作流绑定到一条规则上——规则是对照实际发生的事情检查的，而不是听任何一个模型怎么说。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/shubharya-os/duet/main/install.sh | sh
```

或者在 Claude Code 里作为插件安装：

```
/plugin marketplace add shubharya-os/duet
/plugin install duet@duet
```

使用你已有的 Claude 和 ChatGPT 订阅——**不需要 API key**。只有一个订阅也能用（例如 `--pair claude:opus+claude:sonnet`）。

## 九个命令，每个都有一条被检查的规则

```bash
duet build "一个记账网页应用"     # 从零开始：先定验收标准，再写测试，最后写代码
duet fix "导出时丢失含逗号的行"    # 测试必须在原始代码上失败，在修复后通过
duet add "报告加一个 --json 选项"  # 必须有一个测试在没有该功能时失败
duet refactor "把 parser.py 拆成两个" # 已有测试一个字节都不能改
duet plan "把存储迁移到 Postgres"  # 双方讨论后只允许修改 PLAN.md
duet review                        # 另一个模型只读地审查你的 diff
duet make "…" --like URL           # 研究参考应用、构建、评估功能对等、洁净室检查
duet jury                          # 模拟的用户、客户、投资人和评委；每个意见都引用产品原文
duet page check index.html         # 网站的门槛：检查桌面和手机宽度下的渲染问题
```

`fix` 会把最终的测试**重放到原始代码的快照上**：如果这些测试在有 bug 的代码上也能通过，它们就没有抓住这个 bug，双方的签字会被撤销。

## 效果

仅凭一句话、从空目录开始，duet 在 **35 分钟**内构建了一个记账网页应用（单页 UI、JSON API、SQLite、CSV 导出）。之后我们亲手攻击它：SQL 注入被当作普通文本存储，存储型 XSS 在真实浏览器中被渲染为文本，CSV 公式注入被化解，`0.1 + 0.2` 的合计恰好是 `0.30`。

## 让它成为默认

```bash
duet skill default    # 撤销：duet skill undefault
```

之后 Claude Code、Codex、Gemini CLI、Antigravity、OpenCode 会把真正的代码修改（修 bug、加功能、重构）交给 duet，而提问和一行小改动仍然直接处理。
