CREATE TABLE IF NOT EXISTS venues (
    local_venue_id BIGINT PRIMARY KEY,
    display_name VARCHAR(200) NOT NULL,
    status VARCHAR(20) NOT NULL,
    token_hmac CHAR(64) NOT NULL,
    token_version BIGINT NOT NULL,
    config_revision BIGINT NOT NULL,
    last_request_id CHAR(36) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_cloud_venue_token (token_hmac),
    INDEX idx_cloud_venue_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS form_tokens (
    token_hmac CHAR(64) PRIMARY KEY,
    local_venue_id BIGINT NOT NULL,
    expires_at DATETIME NOT NULL,
    consumed_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cloud_form_expiry (expires_at, consumed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS retired_venue_tokens (
    token_hmac CHAR(64) PRIMARY KEY,
    local_venue_id BIGINT NOT NULL,
    token_version BIGINT NOT NULL,
    retired_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cloud_retired_venue (local_venue_id, retired_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    bucket_key CHAR(64) PRIMARY KEY,
    window_started_at DATETIME NOT NULL,
    request_count INT UNSIGNED NOT NULL DEFAULT 0,
    expires_at DATETIME NOT NULL,
    INDEX idx_cloud_rate_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS submissions (
    submission_id CHAR(36) PRIMARY KEY,
    local_venue_id BIGINT NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'queued',
    encrypted_payload MEDIUMTEXT NOT NULL,
    wrapped_data_key TEXT NOT NULL,
    key_id VARCHAR(100) NOT NULL,
    algorithm_version VARCHAR(100) NOT NULL,
    payload_nonce VARCHAR(100) NOT NULL,
    ciphertext_sha256 CHAR(64) NOT NULL,
    photo_object_key VARCHAR(200) NOT NULL,
    photo_nonce VARCHAR(100) NOT NULL,
    photo_ciphertext_sha256 CHAR(64) NOT NULL,
    photo_size BIGINT UNSIGNED NOT NULL,
    photo_mime_type VARCHAR(100) NOT NULL,
    lease_id CHAR(36) DEFAULT NULL,
    lease_owner VARCHAR(100) DEFAULT NULL,
    lease_expires_at DATETIME DEFAULT NULL,
    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    safe_reason_code VARCHAR(100) DEFAULT NULL,
    received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acknowledged_at DATETIME DEFAULT NULL,
    expires_at DATETIME NOT NULL,
    INDEX idx_cloud_submission_delivery (state, lease_expires_at, received_at),
    INDEX idx_cloud_submission_expiry (expires_at, state),
    INDEX idx_cloud_submission_venue (local_venue_id, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS delivery_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    submission_id CHAR(36) DEFAULT NULL,
    event_type VARCHAR(50) NOT NULL,
    worker_digest CHAR(64) NOT NULL DEFAULT '',
    result_class VARCHAR(50) NOT NULL DEFAULT '',
    safe_reason_code VARCHAR(100) DEFAULT NULL,
    duration_ms INT UNSIGNED NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cloud_delivery_submission (submission_id, created_at),
    INDEX idx_cloud_delivery_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS internal_request_nonces (
    nonce_hash CHAR(64) PRIMARY KEY,
    request_id CHAR(36) NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_cloud_nonce_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS internal_request_results (
    request_id CHAR(36) PRIMARY KEY,
    operation VARCHAR(100) NOT NULL,
    response_json MEDIUMTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    INDEX idx_cloud_request_result_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
