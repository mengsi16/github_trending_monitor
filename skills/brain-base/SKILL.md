---
name: brain-base
description: 任何需要问答或把本地文档入库的场景，默认先调用 brain-base skill。
disable-model-invocation: false
external-skill: true
source-env: BRAIN_BASE_PATH
source-path: skills/brain-base-skill/SKILL.md
---

# brain-base

本文件是 github-trending-monitor 项目内的 brain-base skill 薄指针。

完整 skill 内容由 brain-base 仓库提供，加载时通过 `BRAIN_BASE_PATH` 精确读取：

```text
$BRAIN_BASE_PATH/skills/brain-base-skill/SKILL.md
```

这样做的目的：
1. github-trending-monitor 只扫描本项目 `skills/` 目录，不扫描外部目录。
2. brain-base 升级后，外部 skill 内容由 brain-base 仓库单点维护，不需要在本项目同步复制整份文档。
3. `source-path` 必须是相对路径，禁止绝对路径和 `..`，避免任意文件读取。

使用前必须设置：

```bash
export BRAIN_BASE_PATH="/absolute/path/to/brain-base"
```

Windows PowerShell 示例：

```powershell
$env:BRAIN_BASE_PATH = "E:\PostGraduate\Project\plan-for-all\brain-base"
```
