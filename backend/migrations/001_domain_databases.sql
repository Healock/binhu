-- Run once with a MySQL administrative account during the staged migration.
-- Existing production volumes do not re-run backend/init.sql automatically.
CREATE DATABASE IF NOT EXISTS PlatformData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS VisitData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS DispatchData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS RegistryData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS WorkflowData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON PlatformData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON VisitData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON DispatchData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON RegistryData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON WorkflowData.* TO 'binhu'@'%';
FLUSH PRIVILEGES;
