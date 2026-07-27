-- 执行前必须先备份 OnlineData、OnlineDataArchive 和 daily_report。
-- 本迁移只扩大字段长度，不删除或改写已有数据。
ALTER TABLE OnlineDataArchive.t_suspect_return_archive
    MODIFY COLUMN `联系号码` VARCHAR(500) NULL;
