# 地址匹配智能化升级

- 当前状态：规划中，Dev 验证前未开始
- 首期环境：Dev 自托管，虚构或脱敏数据，CPU 验证
- 首期选型：MGeo、BGE-small-zh、Qdrant、BGE-reranker 和独立校准器
- 生产边界：不接入生产，不接入预发布，不使用外部模型 API

## 目标与处理链路

在现有本地 `RuleMatcher` 之上增加地址预处理、向量召回、候选重排和校准能力。MySQL 仍是业务唯一真相，Qdrant 只保存检索索引和必要候选元数据。

```text
MGeo 预处理和结构化
→ RuleMatcher 硬约束
→ BGE-small-zh 向量生成
→ Qdrant Top-K 召回
→ BGE-reranker 重排
→ 校准器
→ 确认地址工作台人工确认
```

MGeo 解析分为 `complete`、`partial`、`failed`。部分解析保留可用字段并降低信息完整度；完全失败仍保留原始脏地址，可进入人工确认和当前社区全部启用小区搜索。解析分数和向量相似度都不能直接称为概率。

规则层继续负责正式社区、街道、小区启用状态、来源、权限、唯一性和冲突约束。人工确认优先级最高，模型不能改写原始地址、正式社区、核查结果或人工确认。

## 模型、索引和版本

首期使用 BGE-small-zh 生成向量，Qdrant 召回 Top-5，BGE-reranker 对候选重排，全部在 Dev CPU 环境验证。Qdrant payload 仅保存小区 ID、社区 ID、启用状态、别名类型、地址特征哈希、解析器版本、模型版本和索引版本。

每次发布绑定完整匹配版本：MGeo parser、Embedding、Qdrant index、Reranker、校准器、RuleMatcher 和决策策略。Embedding 变化必须重建索引；Reranker 变化必须重新验证校准器；旧版本保留至新版本验收完成。

## 数据去重与匹配率报告

验收同时输出任务级和地址簇级报告。地址簇由 `parser_type + 规范化地址特征 + 正式社区` 组成，使用 HMAC 或安全标识，不在普通报告中输出完整地址。

报告至少包含：原始任务数、有效地址数、地址簇数、重复任务数和重复率、地址簇内任务分布、社区一致率、小区一致率、人工确认一致率、模型建议一致率、Rule-only 与 Rule+Vector 差异、多个确认小区冲突数、空地址、低信息地址和解析失败数量。

同一地址的不同写法归入同一地址簇；仅相似但无法证明相同的地址不强行合并；跨社区同名地址不得合并。一个地址簇内的任务必须同时报告，不能只抽一条掩盖召回退化或人工结果不一致。

## 人工反馈、校准与 Fine-tune

人工确认形成三条反馈路径：相同规范化地址和正式社区的精确反馈记忆可即时复用；校准器按批次学习原始分数与实际正确率的关系；积累足够高质量样本后，BGE-small-zh 和 BGE-reranker 按批次离线 Fine-tune。Fine-tune 和校准器可以并行存在，但独立训练、独立验证，绑定同一匹配版本发布。

训练和校准数据只使用明确人工确认及明确人工“无匹配小区”结论。自动匹配结果未经抽样复核不得直接作为正样本。反馈冲突、来源异常、社区未确定和无法解释的解析失败不直接进入训练集。同一地址簇必须位于同一数据拆分中，训练、验证、校准和独立测试集互斥。

人工反馈标签包括 `positive_match`、`negative_candidate`、`manual_unmatched`、`community_conflict`、`source_exception` 和 `feedback_conflict`。模型训练离线执行，不在在线请求中实时训练。模型退化时拒绝发布并回滚旧版本。

## 反馈冲突和工作台行为

同一规范化地址和正式社区被确认到不同小区时，标记 `feedback_conflict` 并停止该地址簇自动反馈复用。工作台显示历史小区名称、确认人和确认时间；有权限用户可以直接确认新的小区，不增加审批流程。新确认写为新的不可变事件，旧记录不删除、不覆盖，并保留前后关系和责任人。冲突状态提供历史查询入口，不会静默抹平。

确认地址工作台只展示地址—小区关系，不展示姓名、身份证号和手机号。规则得分为 0 时显示“未命中规则”，模型建议使用高/中/低相似度等级，不把未经校准的分数显示为百分比。无候选时可搜索当前社区全部启用小区，并可记录“无匹配小区”。

## Dev 验证通过标准

以下条件必须全部满足才算通过：

- 数据去重、地址簇和任务级数量守恒报告完整；
- MGeo 完整、部分、失败三级统计完整，失败任务均有人工处理路径；
- Top-1、Top-3、Top-5、MRR、人工接受率和人工改判率可复现；
- 地址簇一致率、跨社区错误、同名小区错误和楼栋房号错误可解释；
- 校准集与训练集隔离，可靠性曲线和 Brier score 等指标可生成；
- 模型、解析器、索引、校准器和规则版本可复现并可回滚；
- Rule-only 回退或人工处理路径可用；
- CPU、内存、P95、P99、超时率和 Qdrant 资源开销在可接受范围；
- Qdrant payload、日志和审计不含敏感正文；
- 无生产、预发布、腾讯或外部 API 调用。

任一关键门槛失败，Dev 验证不通过，不进入预发布。

## 预发布 75 人趋势复测

进入预发布后增加一次 75 人、5 分钟突发复测，使用脱敏副本和可清理数据卷。记录整体接口和地址匹配服务 P50/P95/P99、MGeo/Qdrant/Reranker 分段耗时、模型错误和超时、Rule-only 回退、CPU/内存、MySQL 锁等待、连接池、死锁和停止写入后的排空时间。

该复测用于长期趋势对比，不直接构成生产容量承诺。每次算法版本变化都与上一版本比较延迟、错误率、人工一致率和重复/旧 revision 风险。性能退化时单独建立性能治理任务，不简单归因于模型或 Flink。

## 明确不做

- 不在生产环境全量启用 MGeo、Qdrant、Embedding 或 Reranker；
- 不使用外部模型 API；
- 不把 Qdrant 作为业务真相；
- 不把未经校准的相似度称为概率；
- 不让模型绕过社区、街道、启用状态、权限和人工确认；
- 不把人工确认模糊推广到相似地址；
- 不在在线请求中实时训练；
- 不把未经复核的自动匹配作为训练正样本；
- 不把姓名、身份证号、手机号和无关正文加入向量；
- 不恢复腾讯文档、腾讯写回或外部数据路径；
- 不在 Dev 验证前进入预发布或生产；
- 不承诺每新增一次人工确认都会立即提升全部任务准确率。

## 实施顺序

1. 保留 RuleMatcher 默认实现并建立 Dev 环境身份、数据和可清理资源检查。
2. 接入 MGeo 解析适配器、BGE-small-zh 向量生成、Qdrant 索引/重建和 BGE-reranker。
3. 建立 Rule + Vector 候选输出、地址簇去重和匹配率报告。
4. 建立人工反馈事件、冲突历史、精确反馈记忆和校准器。
5. 积累高质量数据后，按批次执行 BGE-small-zh 与 BGE-reranker Fine-tune。
6. 在确认地址工作台展示模型建议和规则依据，完成 Dev 验证。
7. Dev 通过后另立预发布部署及 75 人趋势复测计划。

建议命令：

```bash
python -m address_matching.normalize --input fixtures/address.jsonl
python -m address_matching.index --model bge-small-zh --qdrant-url http://qdrant:6333
python -m address_matching.evaluate --dataset artifacts/address-eval.jsonl --top-k 5
python -m address_matching.deduplicate --input artifacts/predictions.jsonl
python -m address_matching.calibrate --predictions artifacts/predictions.jsonl
python -m address_matching.finetune --config configs/address-matching-bge-small.yaml
python -m address_matching.verify --model-version address-match-vN
```

上述命令首期只允许访问 Dev 数据目录和 Dev Qdrant，执行前必须验证环境身份，禁止指向生产。
