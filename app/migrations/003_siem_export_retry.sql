ALTER TABLE enterprise_intel_reports
    ADD COLUMN IF NOT EXISTS siem_export_status VARCHAR(16) NOT NULL DEFAULT 'NOT_SENT',
    ADD COLUMN IF NOT EXISTS siem_export_attempted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS siem_export_error TEXT;

UPDATE enterprise_intel_reports
SET siem_export_status = CASE WHEN pushed_to_siem THEN 'SENT' ELSE 'NOT_SENT' END
WHERE siem_export_status = 'NOT_SENT' AND pushed_to_siem = TRUE;

CREATE INDEX IF NOT EXISTS idx_reports_siem_retry
ON enterprise_intel_reports(review_status, siem_export_status)
WHERE review_status = 'APPROVED' AND pushed_to_siem = FALSE;
