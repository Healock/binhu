import { useState } from 'react'
import { Alert, Button, Checkbox, Modal, Spin } from 'antd'
import { SettingOutlined } from '@ant-design/icons'
import {
  getSummaryReportConfig,
  updateSummaryReportConfig,
} from '../api/client'

export default function SummaryReportConfigButton() {
  const [open, setOpen] = useState(false)
  const [availableTypes, setAvailableTypes] = useState<string[]>([])
  const [selectedTypes, setSelectedTypes] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const show = async () => {
    setOpen(true)
    setLoading(true)
    setMessage('')
    setError('')
    try {
      const config = await getSummaryReportConfig()
      setAvailableTypes(config.available_types)
      setSelectedTypes(config.selected_types)
    } catch (reason: any) {
      setError(
        reason?.response?.data?.detail || '总汇总表配置加载失败，请稍后重试',
      )
    } finally {
      setLoading(false)
    }
  }

  const save = async () => {
    if (!selectedTypes.length) return
    setSaving(true)
    setMessage('')
    setError('')
    try {
      const config = await updateSummaryReportConfig(selectedTypes)
      setAvailableTypes(config.available_types)
      setSelectedTypes(config.selected_types)
      setMessage(config.message || '总汇总表配置已保存')
    } catch (reason: any) {
      setError(reason?.response?.data?.detail || '保存失败，请稍后重试')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <Button
        size="large"
        icon={<SettingOutlined />}
        onClick={show}
      >
        总汇总配置
      </Button>
      <Modal
        open={open}
        title="总汇总表配置"
        okText="保存配置"
        cancelText="关闭"
        confirmLoading={saving}
        okButtonProps={{ disabled: loading || selectedTypes.length === 0 }}
        onOk={save}
        onCancel={() => setOpen(false)}
      >
        <p className="mb-4 text-sm text-slate-500">
          选择生成总汇总表时要合并的分汇总表。修改后，下一次生成总汇总表时生效。
        </p>
        {loading ? (
          <div className="flex min-h-28 items-center justify-center">
            <Spin />
          </div>
        ) : (
          <Checkbox.Group
            value={selectedTypes}
            onChange={values => setSelectedTypes(values as string[])}
            className="grid gap-3 sm:grid-cols-2"
          >
            {availableTypes.map(parserType => (
              <Checkbox key={parserType} value={parserType}>
                {parserType}
              </Checkbox>
            ))}
          </Checkbox.Group>
        )}
        {message && (
          <Alert className="mt-4" type="success" showIcon message={message} />
        )}
        {error && (
          <Alert className="mt-4" type="error" showIcon message={error} />
        )}
      </Modal>
    </>
  )
}
