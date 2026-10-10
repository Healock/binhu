# 户号表重复地址误判修补（2026-10-10）

## 根因及只读证据

Production 0.31.0 的户号导入按全局标准化地址分组，忽略社区及不同户号；同户号冲突又重复计入问题数。批量新增后的来源回挂同样以地址定位，不能只放宽预览。

2026-10-10 固定注册生产目标的一次只读事务，读取 batch 19 聚合：66223 行、66218 个社区及户号键、0 个空户号、25304 条重复地址问题，另有 5 组同户号冲突共 10 条。没有输出户号、地址或人员正文。连接已关闭。

随后使用本 PR 候选纯分类器在服务器进程内只读计算同一批次：正常 66213、问题 10、冲突组 5、完全一致重复行 0；没有确认导入或写入生产。

复核时发现用户批次已变为 partially_imported，40603 条来源已关联；新增待核查原因：314 条社区无法对应启用正式社区或别名，1 条同户号地址变化，1 条现有档案歧义。当前待核查共 25630，不能把这些原因伪装成重复地址，也不能承诺部署后最终清单只有 10 条。修补后文件预览预期 10；批次待核查预计 326（真实环境部署后再核对）。

## 修补范围

- 同社区及户号识别身份；不同户号同地址、跨社区同地址分别保留注销状态。
- 无户号时保留同社区地址歧义门禁；同户号地址变化、重复现有户号及更新批次覆盖门禁保持。
- 已有档案以户号优先定位；另一户号不作覆盖目标。多个来源户号共享地址时不争用无户号历史档案。
- 新增来源按本次插入 ID 范围及来源引用精确回挂，匹配不唯一时事务回滚。
- Production 启动时对 preview/partially_imported 批次重算已知旧地址问题；仅修改问题和批次计数。旧问题标记 superseded，来源、正式档案、已导入数、人工决定、其他待核查原因保留。不对 imported 批次执行。
- 同文件重复预览及确认也复用上述修补，事务和批次行锁串行化；重复执行无新增问题。

## 验证与发布边界

- `cd backend; python -m pytest tests/test_household_multi_file_import.py tests/test_household_preview_repair.py tests/test_household_cancellation_status.py tests/test_registry_foundation.py tests/test_registry_import_batching.py tests/test_registry_certificate_apply.py tests/test_registry_certificate_comparison.py tests/test_registry_visit_history.py tests/test_property_annotation_xlsx.py tests/test_help_docs.py -q`：154 passed。
- `cd backend; python -m unittest discover -s tests`：1027 passed。
- `cd backend; python -m compileall -q .`：通过。
- 帮助 Markdown 加载检查 5 passed；`git diff --check` 通过。
- 自动化为虚构夹具；真实 MySQL 确认导入事务及多户号新增尚未验证。真实数据只读分类已经核对，不能代替写入验收。

用户已授权提交、合并、部署后端。版本保持 0.31.0，按精确提交追溯此次后端修补，不触发客户端发布、不创建或移动 v0.31.0 标签。部署使用固定 Deploy production、backup_scope=all、release_scope=backend，保留 RegistryData 备份及此前程序回滚制品。无 schema 变更、外部平台调用、客户端变更或正式房屋自动导入。

程序回滚保留 superseded 历史问题和批次来源；回退到旧错误分类器不自动恢复旧阻塞问题。任何数据恢复需要按备份及审计另行确认，禁止自动覆盖当前正式房屋。

## 待追加证据

PR、CI、合并 SHA、部署 Workflow、备份和部署后只读问题数待实际执行后追加。当前文档不宣称已经合并、部署或完成业务验收。
