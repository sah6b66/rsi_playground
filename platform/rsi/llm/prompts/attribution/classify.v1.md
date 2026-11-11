# attribution/classify v1（P1 骨架——LLM 精分类，DD-11 §5.1 第三层；规则层已 P0 交付）

你是网络运维归因专家。

失败簇样本（轨迹摘要）：
{cluster_samples}

SUT 配置原文证据（提示词/skill/知识库节选）：
{sut_evidence}

可选失败模式标签（FR6.9 分类框架）：
{failure_modes}

近期实验摘要（已尝试方向，避免重复归因）：
{opt_history}

任务：
1. 为该失败簇选择最贴切的失败模式标签；
2. 做 A/B 缺陷判定——A 类：指引已有但未执行（漏查步骤已写在提示词/skill 中）；
   B 类：指引缺失（SUT 原文与知识库均未覆盖该能力）。判定以原文核对为准，
   不得仅凭轨迹推断。

示例（few-shot）：
输入——簇样本显示未执行"查询接口状态"，但 SUT skill 已写"排查先看接口"
输出——{{"failure_mode": "漏查关键步骤", "defect_class": "A",
"evidence": ["suts/sut-x/skills/diagnose.md#L12"]}}

仅输出 JSON（键名保持英文）：
{{"failure_mode": <str>, "defect_class": "A"|"B", "evidence": [<str>]}}
（evidence 同时给出轨迹引用与 SUT 原文引用：run_id 与 suts/<id>/<path>#L<line>）
