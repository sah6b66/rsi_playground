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
3. switch_model 类型时 diff_content 为空、model_config_patch 填充；其余类型相反。

仅输出 JSON（键名保持英文）：
{{"problem_trace_ids": [<str>], "failure_type": <str>,
"defect_class": "A"|"B", "proposal_type": <str>, "target_path": <str>,
"diff_content": <str>, "model_config_patch": <str|null>, "rationale": <str>}}
