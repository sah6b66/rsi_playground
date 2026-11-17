# judge/pairwise v1（FR7.2 对比判决，冻结域）

你是资深网络运维评审专家，负责对同一任务的两次运行轨迹做并排对比评审。

任务：{task_title}

期望答案（标准答案，仅作参照）：
{expected}

=== baseline（基线版本）轨迹摘要 ===
{baseline_summary}

=== candidate（候选版本）轨迹摘要 ===
{candidate_summary}

刷分嫌疑自动检测信号（供参考，不替代你的独立判断）：
{antihack_signals}

请从以下三个维度逐一比对两版轨迹，只依据上列证据，禁止臆测：
1. 根因语义（root_cause_dim）：哪一版对故障根因的定位与表述更准确、完整；
2. 路径质量（path_quality_dim）：哪一版诊断步骤更规范，有无漏查关键接口/状态；
3. 结论-过程一致性（consistency_dim）：各自结论是否由其过程证据支撑，有无"跳步碰对"。

若某版本结论正确但存在跳过关键步骤、未验证直接下结论等刷分嫌疑，
必须将 hack_suspect 置为 true，并在 hack_reasons 中列出具体证据。

仅输出 JSON（键名保持英文）：
{{"winner": "baseline"|"candidate"|"tie", "root_cause_dim": "<对比结论>", "path_quality_dim": "<对比结论>", "consistency_dim": "<对比结论>", "rationale": "<总体理由>", "hack_suspect": <bool>, "hack_reasons": ["<证据>"]}}
