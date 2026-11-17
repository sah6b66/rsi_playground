# hypothesis/propose v1（P1 骨架——改进提案，DD-11 §5.2 stage2；P0 由人工表单承担）

你是网络运维自优化的改进提案专家。

问题单：
{problem_statement}

可改要素原文（suspected_elements 定位文件的完整内容，写 diff 须基于原文而非摘要）：
{element_texts}

白名单内可改要素清单（仅允许修改此处列出的要素）：
{enabled_targets}

失败假设摘要（已尝试且无效的方向，不得重复提出）：
{failed_hypotheses}

任务：提出一个改进假设。约束：
1. 只允许修改 enabled_targets 清单内的要素（单点变更，不做复合修改）；
2. 与失败假设摘要中的方向不得重复；
3. diff_content 填基于可改要素原文的 unified diff；switch_model 等无文件改动
   的提案类型留空字符串；
4. expected_improve 一句话给出预期指标改善；risk 说明实施风险与回滚方式；
   verification_task_ids / predicted_affected_tasks 给出建议复验与可能受影响
   的任务 ID（无则空数组）。

仅输出 JSON（键名保持英文，九键齐备）：
{{"problem_trace_ids": [<str>], "failure_type": <str>,
"defect_class": "A"|"B", "proposal_type": <str>, "diff_content": <str>,
"expected_improve": <str>, "risk": <str>, "verification_task_ids": [<str>],
"predicted_affected_tasks": [<str>]}}
