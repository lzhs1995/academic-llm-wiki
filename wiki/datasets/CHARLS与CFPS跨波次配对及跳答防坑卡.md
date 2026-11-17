---
type: dataset
title: CHARLS与CFPS跨波次配对及跳答防坑卡
survey: CHARLS_CFPS
waves:
- CHARLS_W1_W6
- CFPS_2012_2022
unit: 父母×波次主表与父母×波次×子女名册槽位
evidence_review: source_checked
evidence:
- source_id: src-5d198bec0e539cc37417
  version: 116938b8ad0220f448edf600d552f2bb70308f1ad25b2bdc9a3b2659a7731bd5
  path: raw/demo/panel_survey_notes.md
  locator: lines:3-4
  quote: 多子女家庭问卷按槽位存储（如部分波次 11 槽、后续波次扩展至 35 槽），同一子女在不同波次可能落在不同槽位；跨期纵向配对严禁直接按槽号硬连。
---

# CHARLS 与 CFPS 跨波次配对及跳答防坑卡

返回 [[wiki/index|Wiki 路由目录]]

1. **跳答路由防丢样**：合成接触/支持等级前必须逐波核对跳答条件（如高频见面者跳过远程联系题），禁止只补最高档而把“每周见面 1–3 次”误判为缺失。
2. **子女名册槽位防漂移**：`source_slot` 仅代表当期问卷行号而非跨期稳定子女 ID；父母—波次层聚合亦须报告子女进入/退出构成变化。

> "多子女家庭问卷按槽位存储（如部分波次 11 槽、后续波次扩展至 35 槽），同一子女在不同波次可能落在不同槽位；跨期纵向配对严禁直接按槽号硬连。" — raw/demo/panel_survey_notes.md
