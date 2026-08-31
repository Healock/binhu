import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, DatePicker, Descriptions, Drawer, Form, Image, Input, Modal, Popconfirm,
  Select, Space, Spin, Tabs, Tag, Upload, message,
} from 'antd'
import type { TableColumnsType } from 'antd'
import type { ReactNode } from 'react'
import { DownloadOutlined, FileImageOutlined, FilterFilled, PlusOutlined, ReloadOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons'
import dayjs, { type Dayjs } from 'dayjs'
import AppTable from '../components/AppTable'
import type { ResponsiveColumns } from '../components/responsiveTable'
import ExternalDataPanel from '../components/ExternalDataPanel'
import { ListToolbar, PageHeader, Panel } from '../components/ui'
import useDebouncedValue from '../hooks/useDebouncedValue'
import {
  formatUTCTime,
  getGridCommunities,
  registryApi,
  type RegistryCertificateSourceRun,
  type RegistryCertificateStatus,
  type RegistryHousingCategory,
  type RegistryImportIssue,
  type RegistryOrganization,
  type RegistryPerson,
  type RegistryProperty,
  type RegistrySmallCommunityOption,
  type RegistryPropertyVisit,
  type WatchCategory,
} from '../api/client'
import { useAuth } from '../context/AuthContext'
import { downloadBlob } from '../utils/fileDownload'

type TabKey = 'properties' | 'people' | 'organizations' | 'merges' | 'candidates' | 'conflicts' | 'issues' | 'imports'
type ModalKind = 'property' | 'person' | 'organization' | 'phone' | 'alias' | 'personRelation' | 'organizationRelation' | 'merge' | 'personTag'

const housingCategoryOptions = [
  { value: '', label: '全部住房类型' },
  { value: 'rental', label: '出租房' },
  { value: 'self_owned', label: '自购房' },
  { value: 'other', label: '其他类型' },
  { value: 'unmarked', label: '未标注类型' },
]

const issueTypeLabels: Record<string, string> = {
  certificate_duplicate: '告知书重复记录',
  certificate_content_conflict: '告知书内容不一致',
  certificate_non_rental: '告知书未匹配出租房',
  household_duplicate: '户号表重复来源',
  household_missing_type: '户号表未标注类型',
  household_community_unresolved: '户号表社区待核对',
}

const issueTypeOptions = Object.entries(issueTypeLabels).map(([value, label]) => ({ value, label }))

const certificateStatusOptions: Array<{ value: RegistryCertificateStatus; label: string }> = [
  { value: '', label: '全部责任书状态' },
  { value: 'normal_signed', label: '正常签署' },
  { value: 'not_required', label: '无需上传告知书' },
  { value: 'not_uploaded', label: '未上传告知书' },
  { value: 'renter_needs_correction', label: '出租人待修正' },
  { value: 'actual_renter_missing', label: '实际出租人未确定' },
  { value: 'multiple_or_conflict', label: '告知书来源待核对' },
  { value: 'not_applicable', label: '非出租房屋' },
]

const certificateStatusColors: Record<Exclude<RegistryCertificateStatus, ''>, string> = {
  normal_signed: 'success',
  not_required: 'blue',
  not_uploaded: 'error',
  renter_needs_correction: 'orange',
  actual_renter_missing: 'purple',
  multiple_or_conflict: 'volcano',
  not_applicable: 'default',
}

const starRatingOptions = ['一星出租房', '二星出租房', '三星出租房', '四星出租房', '五星出租房']
const addressMatchStatusOptions = [
  { value: 'unmatched', label: '未关联小区', color: 'default' },
  { value: 'suggested', label: '自动匹配', color: 'blue' },
  { value: 'ambiguous', label: '待人工确认', color: 'gold' },
  { value: 'conflict', label: '地址冲突', color: 'error' },
  { value: 'confirmed', label: '已人工确认', color: 'success' },
  { value: 'invalid', label: '低信息地址', color: 'default' },
  { value: 'disabled', label: '小区已停用', color: 'warning' },
]

function addressMatchStatusView(status: string) {
  return addressMatchStatusOptions.find(item => item.value === status)
    || { value: status, label: status || '未关联小区', color: 'default' }
}

function issuePayloadText(row: RegistryImportIssue, ...keys: string[]) {
  for (const key of keys) {
    const value = row.payload?.[key]
    if (value !== undefined && value !== null && String(value).trim()) return String(value).trim()
  }
  return ''
}

export default function RegistryManagement() {
  const { user, systemTimezone } = useAuth()
  const [tab, setTab] = useState<TabKey>('properties')
  const [properties, setProperties] = useState<RegistryProperty[]>([])
  const [people, setPeople] = useState<RegistryPerson[]>([])
  const [organizations, setOrganizations] = useState<RegistryOrganization[]>([])
  const [merges, setMerges] = useState<any[]>([])
  const [candidates, setCandidates] = useState<any[]>([])
  const [conflicts, setConflicts] = useState<any[]>([])
  const [issues, setIssues] = useState<RegistryImportIssue[]>([])
  const [issueType, setIssueType] = useState('')
  const [issueStatus, setIssueStatus] = useState('pending')
  const [issueSourceType, setIssueSourceType] = useState<'' | 'household' | 'certificate'>('')
  const [communityId, setCommunityId] = useState<number | undefined>()
  const [housingCategory, setHousingCategory] = useState<RegistryHousingCategory>('')
  const [certificateStatus, setCertificateStatus] = useState<RegistryCertificateStatus>('')
  const [propertyStatus, setPropertyStatus] = useState<'' | 'active' | 'inactive'>('active')
  const [visitDateRange, setVisitDateRange] = useState<[string, string] | undefined>()
  const [starRatings, setStarRatings] = useState<string[]>([])
  const [smallCommunityOptions, setSmallCommunityOptions] = useState<RegistrySmallCommunityOption[]>([])
  const [smallCommunityIds, setSmallCommunityIds] = useState<number[]>([])
  const [addressMatchStatuses, setAddressMatchStatuses] = useState<string[]>([])
  const [selectedPropertyIds, setSelectedPropertyIds] = useState<number[]>([])
  const [matchConfirmProperty, setMatchConfirmProperty] = useState<RegistryProperty | null>(null)
  const [matchConfirmEntryId, setMatchConfirmEntryId] = useState<number | undefined>()
  const [propertySort, setPropertySort] = useState<'id_desc' | 'address_asc' | 'community_asc' | 'updated_desc' | 'visit_desc'>('id_desc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [matchStatusCounts, setMatchStatusCounts] = useState<Record<string, number>>({})
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importPreview, setImportPreview] = useState<any>(null)
  const [importing, setImporting] = useState(false)
  const [certificateStarting, setCertificateStarting] = useState(false)
  const [certificateRun, setCertificateRun] = useState<RegistryCertificateSourceRun | null>(null)
  const [communities, setCommunities] = useState<Array<{ id: number; name: string }>>([])
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState('')
  const [keyword, setKeyword] = useState('')
  const [keywordFlush, setKeywordFlush] = useState(0)
  const debouncedKeyword = useDebouncedValue(keyword.trim(), 350, keywordFlush)
  const [modal, setModal] = useState<ModalKind | null>(null)
  const [selected, setSelected] = useState<any>(null)
  const [detailKind, setDetailKind] = useState<'property' | 'person' | 'organization' | null>(null)
  const [roleTypes, setRoleTypes] = useState<Array<{ id: number; name: string; subject_type: 'person' | 'organization'; is_active: boolean }>>([])
  const [watchCategories, setWatchCategories] = useState<WatchCategory[]>([])
  const [categoryIds, setCategoryIds] = useState<number[]>([])
  const [detail, setDetail] = useState<any>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [propertyVisits, setPropertyVisits] = useState<RegistryPropertyVisit[]>([])
  const [propertyVisitTotal, setPropertyVisitTotal] = useState(0)
  const [propertyVisitPage, setPropertyVisitPage] = useState(1)
  const [propertyVisitLoading, setPropertyVisitLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [certificateImageLoading, setCertificateImageLoading] = useState<number | null>(null)
  const [certificatePreview, setCertificatePreview] = useState<{ url: string; title: string } | null>(null)
  const listRequestId = useRef(0)
  const certificateRunRef = useRef<RegistryCertificateSourceRun | null>(null)
  const [form] = Form.useForm()
  const canManage = user?.permissions?.includes('registry.property.manage')
  const canReview = user?.permissions?.includes('registry.import.manage')
  const canViewTags = Boolean(user?.permissions?.includes('registry.watch.view'))
  const canManageTags = Boolean(user?.permissions?.includes('registry.watch.manage'))

  useEffect(() => () => {
    if (certificatePreview?.url) URL.revokeObjectURL(certificatePreview.url)
  }, [certificatePreview])

  const openCertificateImage = async (certificate: any) => {
    if (!detail?.id || !certificate?.id) return
    setCertificateImageLoading(certificate.id)
    try {
      const blob = await registryApi.certificateImage(detail.id, certificate.id)
      if (!blob.type.startsWith('image/')) throw new Error('invalid image')
      setCertificatePreview({
        url: URL.createObjectURL(blob),
        title: `责任告知书 · ${detail.normalized_address || detail.natural_address || ''}`,
      })
    } catch (reason: any) {
      let detailMessage = reason?.response?.data?.detail
      if (reason?.response?.data instanceof Blob) {
        try {
          const payload = JSON.parse(await reason.response.data.text())
          detailMessage = payload?.detail
        } catch {
          detailMessage = ''
        }
      }
      message.error(detailMessage || '责任告知书图片读取失败')
    } finally {
      setCertificateImageLoading(null)
    }
  }

  const load = async () => {
    const requestId = ++listRequestId.current
    setLoading(true)
    try {
      if (tab === 'properties') {
        const response = await registryApi.properties({
          keyword: debouncedKeyword,
          community_id: communityId,
          housing_category: housingCategory,
          certificate_status: certificateStatus,
          status: propertyStatus,
          visit_start_date: visitDateRange?.[0],
          visit_end_date: visitDateRange?.[1],
          star_ratings: starRatings,
          small_community_ids: smallCommunityIds,
          address_match_statuses: addressMatchStatuses,
          sort: propertySort,
          page,
          page_size: pageSize,
        })
        if (requestId !== listRequestId.current) return
        setProperties(response.data)
        setTotal(response.total)
        setMatchStatusCounts(response.match_status_counts || {})
      }
      if (tab === 'people') {
        const response = debouncedKeyword || categoryIds.length > 0
          ? await registryApi.searchPeople({ name: debouncedKeyword, category_ids: categoryIds, page: 1, page_size: 100 })
          : await registryApi.people({ category_ids: categoryIds, page_size: 100 })
        if (requestId !== listRequestId.current) return
        setPeople(response.data)
        setTotal(response.total)
      }
      if (tab === 'organizations') {
        const response = await registryApi.organizations({ keyword: debouncedKeyword, page_size: 100 })
        if (requestId !== listRequestId.current) return
        setOrganizations(response.data)
        setTotal(response.total)
      }
      if (tab === 'merges') {
        const response = await registryApi.mergeHistory({ page_size: 100 })
        if (requestId !== listRequestId.current) return
        setMerges(response.data || [])
        setTotal(response.total || response.data?.length || 0)
      }
      if (tab === 'candidates') {
        const response = await registryApi.candidates()
        if (requestId !== listRequestId.current) return
        setCandidates(response.data || [])
        setTotal(response.total || response.data?.length || 0)
      }
      if (tab === 'conflicts') {
        const response = await registryApi.conflicts()
        if (requestId !== listRequestId.current) return
        setConflicts(response.data || [])
        setTotal(response.total || response.data?.length || 0)
      }
      if (tab === 'issues') {
        const response = await registryApi.importIssues({
          keyword: debouncedKeyword,
          status: issueStatus as '' | 'pending' | 'resolved' | 'dismissed',
          issue_type: issueType,
          source_type: issueSourceType,
          community_id: communityId,
          housing_category: housingCategory,
          page,
          page_size: pageSize,
        })
        if (requestId !== listRequestId.current) return
        setIssues(response.data || [])
        setTotal(response.total)
      }
      if (tab === 'imports') setTotal(importPreview?.total_count || 0)
      setError('')
    } catch (reason: any) {
      if (requestId === listRequestId.current) setError(reason?.response?.data?.detail || '辖区档案读取失败')
    } finally {
      if (requestId === listRequestId.current) setLoading(false)
    }
  }

  useEffect(() => {
    void getGridCommunities().then(items => setCommunities(items.filter(item => item.is_active))).catch(() => undefined)
    void registryApi.roleTypes().then(response => setRoleTypes(response.data.filter(item => item.is_active))).catch(() => undefined)
    if (canViewTags) {
      void registryApi.watchCategories().then(response => setWatchCategories(response.data.filter(item => item.is_active))).catch(() => undefined)
    } else {
      setWatchCategories([])
      setCategoryIds([])
    }
  }, [canViewTags])
  useEffect(() => {
    setPage(1)
  }, [tab, debouncedKeyword, categoryIds, issueType, issueStatus, issueSourceType, communityId, housingCategory, certificateStatus, propertyStatus, visitDateRange, starRatings, smallCommunityIds, addressMatchStatuses, propertySort])
  useEffect(() => { void load() }, [tab, debouncedKeyword, categoryIds, issueType, issueStatus, issueSourceType, communityId, housingCategory, certificateStatus, propertyStatus, visitDateRange, starRatings, smallCommunityIds, addressMatchStatuses, propertySort, page, pageSize])

  useEffect(() => {
    if (tab !== 'properties') return
    void registryApi.smallCommunityOptions(communityId)
      .then(result => setSmallCommunityOptions(result.data || []))
      .catch(() => setSmallCommunityOptions([]))
  }, [tab, communityId])

  const applyCertificateRun = (run: RegistryCertificateSourceRun, announce = false) => {
    const previousStatus = certificateRunRef.current?.status
    certificateRunRef.current = run
    setCertificateRun(run)
    if (run.status === 'completed' && run.batch_id && run.preview) {
      const preview = { ...run.preview, batch_id: run.batch_id, source_type: 'certificate' }
      setImportPreview(preview)
      setTotal(preview.total_count || run.accepted_count)
      if (announce && previousStatus !== 'completed') {
        message.success(`告知书读取完成：${preview.normal_count || 0} 条可挂载，${preview.problem_row_count || 0} 条需核查`)
      }
    }
    if (run.status === 'failed' && announce && previousStatus !== 'failed') {
      message.error(run.error_message || '告知书读取失败，已保留读取进度')
    }
  }

  useEffect(() => {
    if (!canReview) return
    let cancelled = false
    void registryApi.latestCertificateSourceRun()
      .then(response => {
        if (!cancelled && response.data) applyCertificateRun(response.data)
      })
      .catch(() => undefined)
    return () => { cancelled = true }
  }, [canReview])

  useEffect(() => {
    if (!certificateRun || !['pending', 'running'].includes(certificateRun.status)) return
    let cancelled = false
    const poll = async () => {
      try {
        const run = await registryApi.certificateSourceRun(certificateRun.id)
        if (!cancelled) applyCertificateRun(run, true)
      } catch {
        // A temporary polling failure does not change the persistent server task.
      }
    }
    const timer = window.setInterval(() => { void poll() }, 1500)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [certificateRun?.id, certificateRun?.status])

  const communityOptions = useMemo(() => communities.map(item => ({ value: item.id, label: item.name })), [communities])

  const filterDropdown = (content: ReactNode, onClear: () => void) => ({
    confirm,
    clearFilters,
  }: { confirm: () => void; clearFilters?: () => void }) => (
    <div className="p-3" style={{ width: 280 }}>
      {content}
      <Space className="mt-3">
        <Button size="small" onClick={() => { onClear(); clearFilters?.(); confirm() }}>清空</Button>
        <Button type="primary" size="small" onClick={() => confirm()}>确定</Button>
      </Space>
    </div>
  )

  const openCreate = (kind: ModalKind) => {
    if (kind !== 'personTag') setSelected(null)
    form.resetFields()
    if (kind === 'property') form.setFieldsValue({ status: 'active' })
    if (kind === 'person') form.setFieldsValue({ verification_status: 'unverified', is_temporary: false })
    if (kind === 'organization') form.setFieldsValue({ organization_type: 'other' })
    if (kind === 'personTag') form.setFieldsValue({ category_id: undefined, basis: '' })
    setModal(kind)
  }

  const openEdit = (kind: 'property' | 'person' | 'organization', row: any) => {
    setSelected(row)
    form.resetFields()
    if (kind === 'property') {
      form.setFieldsValue({
        street: row.street, community_id: row.community_id, natural_address: row.natural_address,
        building: row.building, room: row.room, housing_type: row.housing_type, residence_type: row.residence_type,
        source_house_no: row.source_house_no, source_updated_at: row.source_updated_at, normalized_address: row.normalized_address,
        change_reason: '',
      })
    } else if (kind === 'person') {
      form.setFieldsValue({ name: row.name, identity_number: row.identity_number, verification_status: row.verification_status, is_temporary: row.is_temporary })
    } else {
      form.setFieldsValue({ name: row.name, organization_type: row.organization_type, license_number: row.license_number, notes: row.notes })
    }
    setModal(kind)
  }

  const openDetail = async (kind: 'property' | 'person' | 'organization', row: any) => {
    setSelected(row)
    setDetailKind(kind)
    setDetailOpen(true)
    setDetail(null)
    setPropertyVisits([])
    setPropertyVisitTotal(0)
    setPropertyVisitPage(1)
    try {
      if (kind === 'property') {
        setPropertyVisitLoading(true)
        const propertyDetail = await registryApi.property(row.id)
        setDetail(propertyDetail)
        try {
          const visits = await registryApi.propertyVisits(row.id, { page: 1, page_size: 20 })
          setPropertyVisits(visits.data)
          setPropertyVisitTotal(visits.total)
        } catch (reason: any) {
          message.error(reason?.response?.data?.detail || '历史走访读取失败')
        }
      } else {
        setDetail(kind === 'person' ? await registryApi.person(row.id) : await registryApi.organization(row.id))
      }
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '档案详情读取失败')
    } finally {
      setPropertyVisitLoading(false)
    }
  }

  const loadPropertyVisits = async (nextPage: number) => {
    if (!detail?.id) return
    setPropertyVisitLoading(true)
    try {
      const response = await registryApi.propertyVisits(detail.id, { page: nextPage, page_size: 20 })
      setPropertyVisits(response.data)
      setPropertyVisitTotal(response.total)
      setPropertyVisitPage(response.page)
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '历史走访读取失败')
    } finally {
      setPropertyVisitLoading(false)
    }
  }

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      if (modal === 'property') {
        const community = communities.find(item => item.id === values.community_id)
        const payload = { ...values, community_name_snapshot: community?.name || selected?.community_name || '' }
        if (selected) await registryApi.updateProperty(selected.id, payload)
        else await registryApi.createProperty(payload)
      } else if (modal === 'person') {
        if (selected) await registryApi.updatePerson(selected.id, values)
        else await registryApi.createPerson(values)
      } else if (modal === 'organization') {
        if (selected) await registryApi.updateOrganization(selected.id, values)
        else await registryApi.createOrganization(values)
      } else if (modal === 'phone' && selected) {
        await registryApi.addPhone(selected.id, values)
      } else if (modal === 'alias' && selected) {
        await registryApi.addPropertyAlias(selected.id, values)
      } else if (modal === 'personRelation' && selected) {
        await registryApi.addPropertyPersonRelation(selected.id, values)
      } else if (modal === 'organizationRelation' && selected) {
        await registryApi.addPropertyOrganizationRelation(selected.id, values)
      } else if (modal === 'merge' && selected) {
        await registryApi.mergePerson(selected.id, values)
      } else if (modal === 'personTag' && selected) {
        await registryApi.addPersonTag(selected.id, values)
      }
      message.success('保存成功')
      setModal(null)
      await load()
      if (detailOpen && selected && detailKind === 'property' && ['alias', 'personRelation', 'organizationRelation'].includes(modal || '')) setDetail(await registryApi.property(selected.id))
      if (detailOpen && selected && detailKind === 'person' && modal === 'phone') setDetail(await registryApi.person(selected.id))
      if (detailOpen && selected && detailKind === 'person' && modal === 'personTag') setDetail(await registryApi.person(selected.id))
      if (detailOpen && selected && detailKind === 'organization' && modal === 'organizationRelation') setDetail(await registryApi.organization(selected.id))
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const releasePersonTag = async (row: any) => {
    if (!selected?.id || !row?.assignment_id) return
    try {
      await registryApi.releasePersonTag(selected.id, row.assignment_id)
      message.success('标签已解除')
      if (selected?.id && detailKind === 'person') setDetail(await registryApi.person(selected.id))
      await load()
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '标签解除失败')
    }
  }

  const reviewCandidate = async (id: number, action: 'accept' | 'reject') => {
    await registryApi.reviewCandidate(id, { action, reason: action === 'accept' ? '采用候选变更' : '不采用候选变更' })
    message.success('候选变更已处理')
    await load()
  }

  const reviewConflict = async (id: number, action: 'accept' | 'reject') => {
    await registryApi.reviewConflict(id, { action, reason: action === 'accept' ? '按审核结果解决' : '忽略该冲突' })
    message.success('冲突已处理')
    await load()
  }

  const previewImport = async () => {
    if (!importFile) return
    setImporting(true)
    try {
      const result = await registryApi.previewHouseholdImport(importFile)
      setImportPreview({ ...result, source_type: 'household' })
      setTotal(result.total_count)
      message.success(`已完成预览：${result.normal_count} 条可导入，${result.issue_count} 条需核查`)
      if (tab !== 'imports') setTab('imports')
    } catch (reason: any) {
      if (reason?.response?.status === 413) {
        message.error('户号表超过服务器当前上传限制，请联系管理员更新服务后重试')
      } else if (reason?.code === 'ECONNABORTED') {
        message.error('户号表预览超时；请勿重复点击，稍后刷新页面确认批次状态')
      } else {
        message.error(reason?.response?.data?.detail || '户号表预览失败')
      }
    } finally {
      setImporting(false)
    }
  }

  const startCertificateSource = async () => {
    setCertificateStarting(true)
    try {
      const run = await registryApi.startCertificateSourceRun()
      applyCertificateRun(run)
      message.success(run.reused ? '已有告知书读取任务正在运行' : '告知书读取任务已开始，可以离开页面后再回来查看')
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '告知书读取任务启动失败')
    } finally {
      setCertificateStarting(false)
    }
  }

  const retryCertificateSource = async (restart: boolean) => {
    if (!certificateRun) return
    setCertificateStarting(true)
    try {
      const run = await registryApi.retryCertificateSourceRun(certificateRun.id, restart)
      applyCertificateRun(run)
      message.success(restart ? '已从第一页重新读取告知书' : '已从已保存进度继续读取告知书')
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '告知书读取任务恢复失败')
    } finally {
      setCertificateStarting(false)
    }
  }

  const confirmImport = async () => {
    if (!importPreview?.batch_id) return
    setImporting(true)
    try {
      const certificate = importPreview.source_type === 'certificate'
      const result = certificate
        ? await registryApi.confirmCertificateImport(importPreview.batch_id)
        : await registryApi.confirmHouseholdImport(importPreview.batch_id)
      message.success(certificate
        ? `已挂载 ${result.imported_count} 条告知书；未匹配出租房的记录已进入核查清单`
        : `已导入 ${result.imported_count} 条房屋档案；问题数据仍保留在核查清单`)
      setImportPreview({ ...importPreview, status: result.status, imported_count: result.imported_count })
      await load()
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '户号表导入失败')
    } finally {
      setImporting(false)
    }
  }

  const endRelation = async (kind: 'person' | 'organization' | 'membership', relation: any) => {
    const payload = { valid_from: relation.valid_from || null, valid_to: new Date().toISOString(), verified: Boolean(relation.verified), ...(kind === 'membership' ? { title: relation.title || '' } : {}) }
    if (kind === 'person') await registryApi.updatePropertyPersonRelation(relation.relation_id, payload)
    if (kind === 'organization') await registryApi.updatePropertyOrganizationRelation(relation.relation_id, payload)
    if (kind === 'membership') await registryApi.updateOrganizationMembership(relation.membership_id, payload)
    message.success('关系已结束，历史记录保留')
    if (detailKind === 'property' && selected) setDetail(await registryApi.property(selected.id))
    if (detailKind === 'organization' && selected) setDetail(await registryApi.organization(selected.id))
  }

  const toggleProperty = async (row: RegistryProperty) => {
    await registryApi.changePropertyStatus(row.id, { status: row.status === 'active' ? 'inactive' : 'active', reason: '人工维护' })
    message.success(row.status === 'active' ? '房屋已停用' : '房屋已启用')
    await load()
  }

  const confirmSuggestedPropertyMatches = async () => {
    const rows = properties.filter(row => selectedPropertyIds.includes(row.id))
    const items = rows
      .filter(row => row.address_match_status === 'suggested' && row.small_community_id)
      .map(row => ({ property_id: row.id, small_community_id: Number(row.small_community_id) }))
    if (!items.length) {
      message.warning('请选择已有唯一系统建议的房屋')
      return
    }
    setSaving(true)
    try {
      const result = await registryApi.confirmPropertySmallCommunities(items)
      message.success(result.message)
      setSelectedPropertyIds([])
      await load()
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '小区归属确认失败')
    } finally {
      setSaving(false)
    }
  }

  const confirmPropertyMatch = async () => {
    if (!matchConfirmProperty || !matchConfirmEntryId) return
    const propertyId = matchConfirmProperty.id
    setSaving(true)
    try {
      const result = await registryApi.confirmPropertySmallCommunities([{
        property_id: propertyId,
        small_community_id: matchConfirmEntryId,
      }])
      message.success(result.message)
      setMatchConfirmProperty(null)
      setMatchConfirmEntryId(undefined)
      await load()
      if (detailOpen && detailKind === 'property' && selected?.id === propertyId) {
        setDetail(await registryApi.property(propertyId))
      }
    } catch (reason: any) {
      message.error(reason?.response?.data?.detail || '小区归属确认失败')
    } finally {
      setSaving(false)
    }
  }

  const exportRegistryRecords = async () => {
    if (!['properties', 'people', 'organizations'].includes(tab)) return
    setExporting(true)
    try {
      const exportName = tab === 'properties' ? '房屋档案' : tab === 'people' ? '人员档案' : '机构档案'
      const blob = tab === 'properties'
        ? await registryApi.exportProperties({
          keyword: debouncedKeyword,
          community_id: communityId,
          housing_category: housingCategory,
          certificate_status: certificateStatus,
          status: propertyStatus,
          visit_start_date: visitDateRange?.[0],
          visit_end_date: visitDateRange?.[1],
          star_ratings: starRatings,
          small_community_ids: smallCommunityIds,
          address_match_statuses: addressMatchStatuses,
          sort: propertySort,
        })
        : tab === 'people'
          ? await registryApi.exportPeople({ name: debouncedKeyword, category_ids: categoryIds })
          : await registryApi.exportOrganizations({ keyword: debouncedKeyword })
      if (await downloadBlob(blob, `${exportName}-${new Date().toISOString().slice(0, 10)}.xlsx`)) {
        message.success('已导出当前筛选结果')
      }
    } catch (reason: any) {
      let detailMessage = reason?.response?.data?.detail
      if (reason?.response?.data instanceof Blob) {
        try { detailMessage = JSON.parse(await reason.response.data.text())?.detail } catch { detailMessage = '' }
      }
      message.error(detailMessage || '辖区档案导出失败')
    } finally {
      setExporting(false)
    }
  }

  const propertyColumns: ResponsiveColumns<RegistryProperty> = [
    {
      title: '社区', dataIndex: 'community_name', width: 130, responsivePriority: 'always',
      filteredValue: communityId ? [communityId] : null,
      filterIcon: filtered => <FilterFilled style={{ color: filtered ? 'var(--ant-color-primary)' : undefined }} />,
      filterDropdown: filterDropdown(
        <Select allowClear showSearch optionFilterProp="label" className="w-full" placeholder="全部社区"
          value={communityId} options={communityOptions} onChange={value => {
            setCommunityId(value)
            setSmallCommunityIds([])
            setSelectedPropertyIds([])
          }} />,
        () => setCommunityId(undefined),
      ),
    },
    {
      title: '小区', key: 'small_community', width: 190, responsivePriority: 'always',
      render: (_, row) => (
        <div className="grid gap-1">
          <strong>{row.small_community_name || '未关联小区'}</strong>
          <Tag color={addressMatchStatusView(row.address_match_status).color}>
            {addressMatchStatusView(row.address_match_status).label}
          </Tag>
        </div>
      ),
    },
    {
      title: '匹配依据', key: 'address_match_reason', width: 260, responsivePriority: 'wide',
      render: (_, row) => (
        <div className="grid gap-1 text-xs">
          <span>{row.address_match_reason || '尚未生成匹配建议'}</span>
          {row.address_match_method && <span className="text-[var(--app-text-muted)]">{row.address_match_method} · {Math.round((row.address_match_score || 0) * 100)} 分</span>}
        </div>
      ),
    },
    { title: '标准详细地址', width: 360, ellipsis: true, responsivePriority: 'always', render: (_, row) => row.natural_address || row.normalized_address },
    { title: '户号', dataIndex: 'source_house_no', width: 150, ellipsis: true, responsivePriority: 'standard', render: value => value || '-' },
    {
      title: '住房类型', dataIndex: 'housing_type', width: 120, responsivePriority: 'standard',
      filteredValue: housingCategory ? [housingCategory] : null,
      filterIcon: filtered => <FilterFilled style={{ color: filtered ? 'var(--ant-color-primary)' : undefined }} />,
      filterDropdown: filterDropdown(
        <Select className="w-full" value={housingCategory || undefined} allowClear placeholder="全部住房类型"
          options={housingCategoryOptions.filter(item => item.value)} onChange={value => setHousingCategory((value || '') as RegistryHousingCategory)} />,
        () => setHousingCategory(''),
      ),
      render: value => value
        ? <Tag color={['个人出租', '单位出租'].includes(value) ? 'blue' : value === '自购房屋' ? 'green' : 'default'}>{value}</Tag>
        : <Tag color="warning">未标注</Tag>,
    },
    { title: '居住处所', dataIndex: 'residence_type', width: 120, responsivePriority: 'wide', render: value => value || '-' },
    {
      title: '责任书', key: 'certificate_status', width: 220, responsivePriority: 'standard',
      filteredValue: certificateStatus ? [certificateStatus] : null,
      filterIcon: filtered => <FilterFilled style={{ color: filtered ? 'var(--ant-color-primary)' : undefined }} />,
      filterDropdown: filterDropdown(
        <Select className="w-full" value={certificateStatus || undefined} allowClear placeholder="全部责任书状态"
          options={certificateStatusOptions.filter(item => item.value)} onChange={value => setCertificateStatus((value || '') as RegistryCertificateStatus)} />,
        () => setCertificateStatus(''),
      ),
      render: (_, row) => (
      <div className="registry-certificate-cell">
        <Tag color={certificateStatusColors[row.certificate_status || 'not_uploaded']}>
          {row.certificate_status_label || '未上传告知书'}
        </Tag>
        {row.certificate_status !== 'not_applicable' && (
          <span>{row.landlord_renter_relation_label || '责任关系待确认'}</span>
        )}
      </div>
      ) },
    {
      title: '最近走访日期', key: 'latest_visit_date', width: 150, responsivePriority: 'always',
      filteredValue: visitDateRange ? ['range'] : null,
      filterIcon: filtered => <FilterFilled style={{ color: filtered ? 'var(--ant-color-primary)' : undefined }} />,
      filterDropdown: filterDropdown(
        <DatePicker.RangePicker className="w-full" value={visitDateRange ? [dayjs(visitDateRange[0]), dayjs(visitDateRange[1])] : null}
          onChange={(values: [Dayjs | null, Dayjs | null] | null) => {
            if (!values?.[0] || !values[1]) setVisitDateRange(undefined)
            else setVisitDateRange([values[0].format('YYYY-MM-DD'), values[1].format('YYYY-MM-DD')])
          }} />,
        () => setVisitDateRange(undefined),
      ),
      render: (_, row) => (
      <div className="registry-visit-cell">
        <strong>{row.latest_visit_date || '暂无走访'}</strong>
      </div>
      ) },
    {
      title: '星级评定', key: 'latest_star_rating', width: 150, responsivePriority: 'always',
      filteredValue: starRatings.length ? starRatings : null,
      filterIcon: filtered => <FilterFilled style={{ color: filtered ? 'var(--ant-color-primary)' : undefined }} />,
      filterDropdown: filterDropdown(
        <Select mode="multiple" allowClear className="w-full" value={starRatings} maxTagCount="responsive"
          placeholder="全部星级评定" options={starRatingOptions.map(value => ({ value, label: value }))}
          onChange={values => setStarRatings(values)} />,
        () => setStarRatings([]),
      ),
      render: (_, row) => row.latest_star_rating
        ? <Tag color="gold">{row.latest_star_rating}</Tag>
        : '-',
    },
    {
      title: '状态', dataIndex: 'status', width: 90, responsivePriority: 'always',
      filteredValue: propertyStatus ? [propertyStatus] : null,
      filterIcon: filtered => <FilterFilled style={{ color: filtered ? 'var(--ant-color-primary)' : undefined }} />,
      filterDropdown: filterDropdown(
        <Select className="w-full" value={propertyStatus || undefined} allowClear placeholder="全部状态"
          options={[{ value: 'active', label: '启用房屋' }, { value: 'inactive', label: '停用房屋' }]}
          onChange={value => setPropertyStatus((value || '') as '' | 'active' | 'inactive')} />,
        () => setPropertyStatus(''),
      ),
      render: value => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? '启用' : '停用'}</Tag>,
    },
    { title: '版本', dataIndex: 'version', width: 80, responsivePriority: 'wide' },
    { title: '操作', key: 'actions', width: 280, render: (_, row) => <Space wrap>
      <Button size="small" onClick={() => openDetail('property', row)}>详情</Button>
      {canManage && <Button size="small" type={row.address_match_status === 'suggested' ? 'primary' : 'default'} onClick={() => {
        setMatchConfirmProperty(row)
        setMatchConfirmEntryId(row.small_community_id || undefined)
      }}>{row.address_match_status === 'confirmed' ? '修正小区' : '确认小区'}</Button>}
      {canManage && <Button size="small" onClick={() => openEdit('property', row)}>编辑</Button>}
      {canManage && <Popconfirm title={row.status === 'active' ? '确认停用这套房屋？' : '确认启用这套房屋？'} onConfirm={() => void toggleProperty(row)}><Button size="small">{row.status === 'active' ? '停用' : '启用'}</Button></Popconfirm>}
    </Space> },
  ]
  const personColumns: TableColumnsType<RegistryPerson> = [
    { title: '姓名', dataIndex: 'name', width: 130 },
    ...(user?.role === 'super_admin' ? [{ title: '身份证号', dataIndex: 'identity_number', width: 210, render: (value: string) => value || '未登记' }] : []),
    ...(canViewTags ? [{
      title: '人员标签', dataIndex: 'categories', width: 260,
      render: (values: RegistryPerson['categories']) => values?.length
        ? <Space size={[4, 4]} wrap>{values.map(item => <Tag key={item.assignment_id} color={item.color}>{item.name}</Tag>)}</Space>
        : <span className="text-[var(--app-text-secondary)]">暂无有效标签</span>,
    }] : []),
    { title: '核实状态', dataIndex: 'verification_status', width: 110 },
    { title: '临时档案', dataIndex: 'is_temporary', width: 100, render: value => value ? <Tag color="gold">是</Tag> : '否' },
    { title: '状态', dataIndex: 'status', width: 90 },
    { title: '操作', key: 'actions', width: 250, render: (_, row) => <Space>
      <Button size="small" onClick={() => openDetail('person', row)}>详情</Button>
      {canManage && <Button size="small" onClick={() => openEdit('person', row)}>编辑</Button>}
      {canManageTags && <Button size="small" onClick={() => { setSelected(row); openCreate('personTag') }}>加标签</Button>}
      {canManage && <Button size="small" onClick={() => { setSelected(row); form.resetFields(); setModal('merge') }}>合并</Button>}
    </Space> },
  ]
  const organizationColumns: TableColumnsType<RegistryOrganization> = [
    { title: '机构名称', dataIndex: 'name', width: 220 },
    { title: '类型', dataIndex: 'organization_type', width: 130 },
    { title: '登记编号', dataIndex: 'license_number', width: 180 },
    { title: '备注', dataIndex: 'notes', ellipsis: true },
    { title: '状态', dataIndex: 'status', width: 90 },
    { title: '操作', key: 'actions', width: 120, render: (_, row) => <Space><Button size="small" onClick={() => openDetail('organization', row)}>详情</Button>{canManage && <Button size="small" onClick={() => openEdit('organization', row)}>编辑</Button>}</Space> },
  ]

  const issueColumns: TableColumnsType<RegistryImportIssue> = [
    { title: '社区', width: 130, render: (_, row) => issuePayloadText(row, 'community', 'community_name', '社区名称', 'sssq') || '-' },
    { title: '房屋地址', width: 320, ellipsis: true, render: (_, row) => issuePayloadText(row, 'address', 'normalized_address', '详细地址') || row.entity_key || '-' },
    { title: '户号', width: 140, ellipsis: true, render: (_, row) => issuePayloadText(row, 'house_no', '户号') || '-' },
    { title: '住房类型', width: 120, render: (_, row) => issuePayloadText(row, 'housing_type', '住房类型') || <Tag color="warning">未标注</Tag> },
    { title: '问题类型', dataIndex: 'issue_type', width: 180, render: value => <Tag color="orange">{issueTypeLabels[value] || value}</Tag> },
    { title: '问题字段与错误值', width: 300, render: (_, row) => (
      <div className="registry-issue-evidence">
        {(row.problem_details || []).map((detail, index) => (
          <div className="registry-issue-evidence__item" key={`${detail.field}-${index}`}>
            <span className="registry-issue-evidence__field">{detail.field}</span>
            <span className="registry-issue-evidence__value" title={detail.value}>{detail.value}</span>
          </div>
        ))}
      </div>
    ) },
    { title: '为什么有问题', dataIndex: 'reason', width: 300, render: value => <span className="registry-issue-reason">{value || '来源数据不符合自动导入规则'}</span> },
    { title: '来源', width: 120, render: (_, row) => <div className="registry-issue-source"><Tag>{row.source_type === 'household' ? '户号表' : '告知书'}</Tag><span>{row.source_ref || '-'}</span></div> },
  ]

  const renderSearchInput = (placeholder: string) => <Input
    allowClear
    prefix={<SearchOutlined />}
    value={keyword}
    onChange={event => setKeyword(event.target.value)}
    onPressEnter={() => setKeywordFlush(current => current + 1)}
    placeholder={placeholder}
    className="w-full md:w-80"
  />

  const toolbarFilters = tab === 'properties' ? <>
    {renderSearchInput('搜索地址、户号、幢室或住房类型')}
    <Select
      mode="multiple"
      allowClear
      showSearch
      optionFilterProp="label"
      value={smallCommunityIds}
      onChange={values => setSmallCommunityIds(values)}
      options={smallCommunityOptions.map(item => ({
        value: item.id,
        label: `${item.name} · ${item.community_name}`,
      }))}
      maxTagCount="responsive"
      placeholder="按小区筛选"
      className="w-full md:w-72"
    />
    <Select
      mode="multiple"
      allowClear
      value={addressMatchStatuses}
      onChange={values => setAddressMatchStatuses(values)}
      options={addressMatchStatusOptions.map(item => ({ value: item.value, label: item.label }))}
      maxTagCount="responsive"
      placeholder="按匹配状态筛选"
      className="w-full md:w-64"
    />
    <Select
      value={propertySort}
      onChange={value => { setPropertySort(value); setPage(1) }}
      options={[
        { value: 'id_desc', label: '默认排序（最新）' },
        { value: 'address_asc', label: '地址升序' },
        { value: 'community_asc', label: '社区升序' },
        { value: 'updated_desc', label: '最近更新' },
        { value: 'visit_desc', label: '最近走访' },
      ]}
      className="w-full md:w-44"
    />
  </> : tab === 'issues' ? <>
    {renderSearchInput('搜索地址、户号、社区或错误值')}
    <Select
      allowClear
      showSearch
      optionFilterProp="label"
      placeholder="全部社区"
      value={communityId}
      onChange={value => setCommunityId(value)}
      options={communityOptions}
      className="w-full md:w-44"
    />
    <Select
      value={issueSourceType}
      onChange={value => setIssueSourceType(value)}
      options={[
        { value: '', label: '全部来源' },
        { value: 'household', label: '户号表' },
        { value: 'certificate', label: '房东责任告知书' },
      ]}
      className="w-full md:w-44"
    />
    <Select
      allowClear
      placeholder="全部问题类型"
      value={issueType || undefined}
      onChange={value => setIssueType(value || '')}
      options={issueTypeOptions}
      className="w-full md:w-56"
    />
    <Select
      value={housingCategory}
      onChange={value => setHousingCategory(value as RegistryHousingCategory)}
      options={housingCategoryOptions}
      className="w-full md:w-40"
    />
    <Select
      value={issueStatus}
      onChange={value => setIssueStatus(value)}
      options={[
        { value: 'pending', label: '待处理' },
        { value: 'resolved', label: '已处理' },
        { value: 'dismissed', label: '已忽略' },
        { value: '', label: '全部状态' },
      ]}
      className="w-full md:w-36"
    />
  </> : ['people', 'organizations'].includes(tab)
    ? <>
      {renderSearchInput(tab === 'people' ? '搜索人员姓名' : '搜索机构名称')}
      {tab === 'people' && canViewTags && <Select
        mode="multiple"
        allowClear
        value={categoryIds}
        onChange={value => setCategoryIds(value)}
        options={watchCategories.map(item => ({ value: item.id, label: item.name }))}
        maxTagCount="responsive"
        placeholder="按人员标签筛选"
        className="w-full md:w-72"
      />}
    </>
    : undefined

  const toolbarNotice = tab === 'issues' ? <Alert
    type="info"
    showIcon
    message="这里展示来源数据中需要在居住证系统修正的房屋"
    description="平台不会直接修改居住证系统。请根据“问题字段与错误值”和原因，到居住证系统更新正确内容；再次导入最新数据后即可重新核对。"
  /> : undefined

  const certificateRunActive = Boolean(certificateRun && ['pending', 'running'].includes(certificateRun.status))
  const certificatePhaseLabel = certificateRun?.phase === 'reading'
    ? `正在读取第 ${certificateRun.current_page + 1} 页`
    : certificateRun?.phase === 'classifying'
      ? '正在分析重复和冲突记录'
      : certificateRun?.phase === 'writing_preview'
        ? '正在生成导入预览'
        : certificateRun?.status === 'pending'
          ? '等待开始读取'
          : certificateRun?.status === 'completed'
            ? '读取完成'
            : certificateRun?.status === 'failed'
              ? '读取中断'
              : ''

  const toolbarActions = <>
    {tab !== 'imports' && <Button icon={<ReloadOutlined />} onClick={() => void load()}>刷新</Button>}
    {['properties', 'people', 'organizations'].includes(tab) && (
      <Button icon={<DownloadOutlined />} loading={exporting} onClick={() => void exportRegistryRecords()}>
        导出当前结果
      </Button>
    )}
    {canManage && tab === 'properties' && selectedPropertyIds.length > 0 && (
      <Popconfirm
        title={`确认当前选择的 ${selectedPropertyIds.length} 套房屋？`}
        description="只会处理已有唯一自动匹配的记录；冲突、未匹配和低信息地址不会进入分配。管理员仍可在详情中修正归属。"
        onConfirm={() => void confirmSuggestedPropertyMatches()}
      >
        <Button type="primary" loading={saving}>批量处理自动匹配</Button>
      </Popconfirm>
    )}
    {canManage && tab === 'properties' && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('property')}>新增房屋</Button>}
    {canManage && tab === 'people' && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('person')}>新增人员</Button>}
    {canManage && tab === 'organizations' && <Button type="primary" icon={<PlusOutlined />} onClick={() => openCreate('organization')}>新增机构</Button>}
    {canReview && tab === 'imports' && <>
      <Upload accept=".xlsx" maxCount={1} showUploadList={Boolean(importFile)} beforeUpload={file => { setImportFile(file); setImportPreview(null); return false }} onRemove={() => { setImportFile(null); setImportPreview(null) }}>
        <Button icon={<UploadOutlined />}>选择户号表</Button>
      </Upload>
      <Button type="primary" onClick={() => void previewImport()} loading={importing} disabled={!importFile}>预览户号表</Button>
      {!certificateRunActive && certificateRun?.status !== 'failed' && <Button onClick={() => void startCertificateSource()} loading={certificateStarting}>读取告知书</Button>}
      {certificateRunActive && <Button loading disabled>{certificatePhaseLabel}</Button>}
      {certificateRun?.status === 'failed' && <>
        {certificateRun.error_code !== 'source_changed' && <Button onClick={() => void retryCertificateSource(false)} loading={certificateStarting}>继续读取</Button>}
        <Popconfirm
          title="确认从第一页重新读取告知书？"
          description="已保存的分页进度会被清除，但不会影响上一次成功的导入预览。"
          onConfirm={() => void retryCertificateSource(true)}
        >
          <Button loading={certificateStarting}>重新读取</Button>
        </Popconfirm>
      </>}
      {importPreview?.status === 'preview' && <Button onClick={() => void confirmImport()} loading={importing}>
        {importPreview.source_type === 'certificate' ? '确认挂载告知书' : '确认导入正常数据'}
      </Button>}
    </>}
  </>

  const listPagination = ['properties', 'issues'].includes(tab) ? {
    current: page,
    pageSize,
    total,
    showSizeChanger: true,
    pageSizeOptions: [20, 50, 100, 200],
    showTotal: (count: number) => `共 ${count} 条`,
    onChange: (nextPage: number, nextPageSize: number) => {
      setPage(nextPageSize === pageSize ? nextPage : 1)
      setPageSize(nextPageSize)
    },
  } : false

  return (
    <div className="registry-management-layout">
      <PageHeader title="辖区档案" description="长期维护辖区房屋、房东、业主、中介和租房平台关系；业务数据只进入待审核变更。" />
      {error && <Alert type="error" showIcon message={error} />}
      <Panel>
        <Tabs
          activeKey={tab}
          onChange={value => {
            setTab(value as TabKey)
            setKeyword('')
            setCommunityId(undefined)
            setSmallCommunityIds([])
            setAddressMatchStatuses([])
            setSelectedPropertyIds([])
            setCategoryIds([])
            setHousingCategory('')
            setCertificateStatus('')
            setPropertyStatus('active')
            setVisitDateRange(undefined)
            setStarRatings([])
            setPropertySort('id_desc')
            setPage(1)
          }}
          items={[
            { key: 'properties', label: '房屋档案' },
            { key: 'people', label: '人员档案' },
            { key: 'organizations', label: '机构档案' },
            ...(canManage ? [{ key: 'merges', label: '合并历史' }] : []),
            ...(canReview ? [{ key: 'candidates', label: '待审核变更' }, { key: 'conflicts', label: '冲突处理' }] : []),
            { key: 'issues', label: '问题数据核查' },
            ...(canReview ? [{ key: 'imports', label: '数据导入' }] : []),
          ]}
        />
        <div className="registry-management__content">
          {tab !== 'imports' && <ListToolbar
            filters={toolbarFilters}
            notice={toolbarNotice}
            meta={tab === 'properties' ? <Space size="middle" wrap>
              <span>当前筛选共 {total} 条</span>
              <span>待确认 {(
                (matchStatusCounts.suggested || 0)
                + (matchStatusCounts.ambiguous || 0)
                + (matchStatusCounts.conflict || 0)
                + (matchStatusCounts.disabled || 0)
              )} 条</span>
              <span>未关联 {(
                (matchStatusCounts.unmatched || 0)
                + (matchStatusCounts.invalid || 0)
              )} 条</span>
            </Space> : <span>当前筛选共 {total} 条</span>}
            actions={toolbarActions}
          />}
          {tab === 'issues' && <AppTable
            rowKey="id"
            loading={loading}
            dataSource={issues}
            pagination={listPagination}
            scroll={{ x: 1620 }}
            columns={issueColumns}
            emptyText="当前筛选条件下没有问题房屋"
          />}
          {tab === 'imports' && <ExternalDataPanel
            embedded
            title="房屋档案外部数据"
            description="户号表用于补充房屋档案，房东责任告知书只挂载到已存在的出租房；预览不会修改正式档案。"
            actions={toolbarActions}
            stats={[
              { label: '本次预览', value: importPreview ? `${importPreview.total_count} 条` : '尚未开始' },
              { label: '来源', value: importPreview?.source_type === 'certificate' ? '房东责任告知书' : importPreview ? '户号表' : '等待选择' },
              { label: '读取方式', value: certificateRun?.trigger_source === 'scheduled' ? '每日自动读取' : certificateRun ? '人工读取' : '尚无任务' },
              { label: '已读取', value: `${certificateRun?.fetched_count || 0} 条` },
              { label: '范围校验通过', value: `${certificateRun?.accepted_count || 0} 条`, hint: `排除 ${certificateRun?.rejected_count || 0} 条` },
            ]}
            progress={certificateRun ? {
              label: <span className="flex items-center gap-2">{certificateRunActive && <Spin size="small" />}{certificatePhaseLabel}</span>,
              status: <Tag color={certificateRun.status === 'completed' ? 'success' : certificateRun.status === 'failed' ? 'error' : 'processing'}>
                {certificateRun.status === 'completed' ? '已完成' : certificateRun.status === 'failed' ? '已中断' : '执行中'}
              </Tag>,
              detail: <>
                {certificateRun.business_date ? `业务日期 ${certificateRun.business_date}` : '等待业务日期'}
                {certificateRun.current_page > 0 && <> · 已保存至第 {certificateRun.current_page} 页</>}
                {certificateRunActive && <> · 可以离开本页面，任务会在服务器继续执行</>}
              </>,
            } : undefined}
          >
            {certificateRun?.status === 'failed' && <Alert
              type="warning"
              showIcon
              message={certificateRun.error_message || '读取中断，已保存当前进度'}
              description={certificateRun.error_code === 'source_changed'
                ? '断点位置的数据已经变化，为避免页码错位，需要从第一页重新读取。'
                : '可以点击“继续读取”从已保存分页继续；如来源数据已经大幅调整，也可以选择重新读取。'}
            />}
            {importPreview ? <Alert type="success" showIcon message={importPreview.source_type === 'certificate'
              ? `告知书共 ${importPreview.total_count} 条；${importPreview.normal_count} 条可尝试挂载；${importPreview.problem_row_count} 条需核查。`
              : `户号表共 ${importPreview.total_count} 条；${importPreview.normal_count} 条可导入；${importPreview.issue_count} 条需核查。`}
              description={importPreview.status === 'preview' ? '当前仍是预览状态，确认后只导入正常数据，问题记录进入“问题数据核查”。' : `处理状态：${importPreview.status}`} />
              : <div className="registry-import-empty">请选择户号表进行预览，或读取房东责任告知书来源。</div>}
          </ExternalDataPanel>}
        {tab === 'properties' && <AppTable
          fitHeight
          rowKey="id"
          loading={loading}
          columns={propertyColumns}
          responsiveDetails
          dataSource={properties}
          pagination={listPagination}
          rowSelection={canManage ? {
            selectedRowKeys: selectedPropertyIds,
            onChange: keys => setSelectedPropertyIds(keys.map(Number)),
            getCheckboxProps: row => ({
              disabled: row.address_match_status !== 'suggested' || !row.small_community_id,
              title: row.address_match_status === 'suggested' && row.small_community_id
                ? '选择后可批量处理自动匹配'
                : '只有唯一自动匹配可批量处理',
            }),
          } : undefined}
          scroll={{ x: 1660 }}
          emptyText="当前筛选条件下没有房屋档案"
        />}
        {tab === 'people' && <AppTable rowKey="id" loading={loading} columns={personColumns} dataSource={people} pagination={false} scroll={{ x: 850 }} />}
        {tab === 'organizations' && <AppTable rowKey="id" loading={loading} columns={organizationColumns} dataSource={organizations} pagination={false} scroll={{ x: 850 }} />}
        {tab === 'merges' && <AppTable rowKey="id" loading={loading} dataSource={merges} pagination={false} columns={[
          { title: '源档案', render: (_: unknown, row: any) => `${row.source_name}（${row.source_person_id}）` },
          { title: '目标档案', render: (_: unknown, row: any) => `${row.target_name}（${row.target_person_id}）` },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
          { title: '时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) },
          { title: '状态', dataIndex: 'undone', width: 90, render: (value: boolean) => value ? <Tag>已撤销</Tag> : <Tag color="orange">已合并</Tag> },
          { title: '操作', width: 110, render: (_: unknown, row: any) => !row.undone && <Popconfirm title="确认撤销这次合并？" onConfirm={async () => { await registryApi.undoMerge(row.id); message.success('合并已撤销'); await load() }}><Button size="small">撤销</Button></Popconfirm> },
        ]} />}
        {tab === 'candidates' && <AppTable rowKey="id" loading={loading} dataSource={candidates} pagination={false} columns={[
          { title: '对象', dataIndex: 'entity_type', width: 120 },
          { title: '变更', dataIndex: 'change_type', width: 120 },
          { title: '原因', dataIndex: 'reason', ellipsis: true },
          { title: '创建时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) },
          { title: '操作', width: 170, render: (_: unknown, row: any) => <Space><Button size="small" type="primary" onClick={() => void reviewCandidate(row.id, 'accept')}>采用</Button><Button size="small" danger onClick={() => void reviewCandidate(row.id, 'reject')}>拒绝</Button></Space> },
        ]} />}
        {tab === 'conflicts' && <AppTable rowKey="id" loading={loading} dataSource={conflicts} pagination={false} columns={[
          { title: '对象', dataIndex: 'entity_type', width: 120 },
          { title: '冲突类型', dataIndex: 'conflict_type', width: 160 },
          { title: '对象键', dataIndex: 'entity_key', ellipsis: true },
          { title: '创建时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) },
          { title: '操作', width: 170, render: (_: unknown, row: any) => <Space><Button size="small" type="primary" onClick={() => void reviewConflict(row.id, 'accept')}>解决</Button><Button size="small" onClick={() => void reviewConflict(row.id, 'reject')}>忽略</Button></Space> },
        ]} />}
        </div>
      </Panel>

      <Modal
        open={Boolean(matchConfirmProperty)}
        title={matchConfirmProperty?.address_match_status === 'confirmed' ? '修正房屋所属小区' : '确认房屋所属小区'}
        okText="确认关联"
        cancelText="取消"
        confirmLoading={saving}
        okButtonProps={{ disabled: !matchConfirmEntryId }}
        onCancel={() => {
          if (saving) return
          setMatchConfirmProperty(null)
          setMatchConfirmEntryId(undefined)
        }}
        onOk={() => void confirmPropertyMatch()}
      >
        <div className="grid gap-4">
          <Alert
            type="info"
            showIcon
            message="人工确认后，规则重跑不会覆盖结果"
            description={matchConfirmProperty?.address_match_reason || '请按房屋标准地址、历史地址和别名核对唯一小区。'}
          />
          <div className="text-sm text-[var(--app-text-secondary)]">
            <strong className="text-[var(--app-text)]">房屋地址：</strong>
            {matchConfirmProperty?.natural_address || matchConfirmProperty?.normalized_address || '未填写'}
          </div>
          <Select
            showSearch
            optionFilterProp="label"
            value={matchConfirmEntryId}
            placeholder="选择当前社区下的小区"
            options={smallCommunityOptions
              .filter(item => item.community_id === matchConfirmProperty?.community_id)
              .map(item => ({
                value: item.id,
                label: `${item.name}${item.detail_address ? ` · ${item.detail_address}` : ''}`,
              }))}
            onChange={value => setMatchConfirmEntryId(value)}
          />
        </div>
      </Modal>

      <Modal open={Boolean(modal)} title={modal === 'property' ? `${selected ? '编辑' : '新增'}房屋档案` : modal === 'person' ? `${selected ? '编辑' : '新增'}辖区人员` : modal === 'organization' ? `${selected ? '编辑' : '新增'}机构档案` : modal === 'phone' ? '添加联系电话' : modal === 'alias' ? '添加地址别名' : modal === 'personRelation' ? '添加房屋人员关系' : modal === 'organizationRelation' ? '添加房屋机构关系' : modal === 'personTag' ? `为${selected?.name || '人员'}添加标签` : '合并人员档案'} onCancel={() => setModal(null)} onOk={() => void save()} confirmLoading={saving} destroyOnClose>
        <Form form={form} layout="vertical" preserve={false}>
          {modal === 'property' && <>
            <Form.Item name="community_id" label="社区" rules={[{ required: true }]}><Select showSearch options={communityOptions} /></Form.Item>
            <Form.Item name="street" label="街道"><Input /></Form.Item>
            <Form.Item name="natural_address" label="自然地址" rules={[{ required: true }]}><Input /></Form.Item>
            <div className="grid grid-cols-2 gap-3"><Form.Item name="building" label="幢"><Input /></Form.Item><Form.Item name="room" label="室"><Input /></Form.Item></div>
            <div className="grid grid-cols-2 gap-3"><Form.Item name="housing_type" label="住房类型"><Input placeholder="个人出租、单位出租、自购房屋、借住、其他、其它" /></Form.Item><Form.Item name="residence_type" label="居住处所"><Input /></Form.Item></div>
            <Form.Item name="source_house_no" label="来源房屋编号"><Input /></Form.Item>
            <Form.Item name="normalized_address" label="标准化地址"><Input.TextArea autoSize /></Form.Item>
            {selected && <Form.Item name="change_reason" label="变更原因"><Input.TextArea autoSize /></Form.Item>}
          </>}
          {modal === 'person' && <>
            <Form.Item name="name" label="姓名" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="identity_number" label="身份证号"><Input /></Form.Item>
            <Form.Item name="verification_status" label="核实状态"><Select options={[{ value: 'unverified', label: '未核实' }, { value: 'pending', label: '待核实' }, { value: 'verified', label: '已核实' }]} /></Form.Item>
          </>}
          {modal === 'personTag' && <>
            <Alert type="info" showIcon message="标签将直接归档到该人员" description="标签仍保留有效期和历史版本，并继续用于在线任务识别。" />
            <Form.Item name="category_id" label="标签分类" rules={[{ required: true, message: '请选择标签分类' }]}>
              <Select showSearch optionFilterProp="label" options={watchCategories.map(item => ({ value: item.id, label: item.name }))} />
            </Form.Item>
            <Form.Item name="basis" label="依据"><Input.TextArea rows={3} placeholder="可填写来源或核实依据" /></Form.Item>
          </>}
          {modal === 'organization' && <>
            <Form.Item name="name" label="机构名称" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="organization_type" label="机构类型"><Select options={[{ value: 'agency', label: '中介' }, { value: 'rental_platform', label: '租房平台' }, { value: 'property', label: '物业' }, { value: 'other', label: '其他' }]} /></Form.Item>
            <Form.Item name="license_number" label="登记编号"><Input /></Form.Item>
            <Form.Item name="notes" label="备注"><Input.TextArea autoSize /></Form.Item>
          </>}
          {modal === 'phone' && <>
            <Form.Item name="phone" label="手机号" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="is_primary" label="主号码" initialValue={false}><Select options={[{ value: false, label: '否' }, { value: true, label: '是' }]} /></Form.Item>
          </>}
          {modal === 'alias' && <Form.Item name="alias" label="地址别名" rules={[{ required: true }]}><Input /></Form.Item>}
          {modal === 'personRelation' && <>
            <Form.Item name="person_id" label="人员" rules={[{ required: true }]}><Select showSearch options={people.filter(item => item.status === 'active').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="role_type_id" label="角色" rules={[{ required: true }]}><Select options={roleTypes.filter(item => item.subject_type === 'person').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="valid_from" label="生效时间"><Input placeholder="留空表示即时生效" /></Form.Item>
          </>}
          {modal === 'organizationRelation' && <>
            <Form.Item name="organization_id" label="机构" rules={[{ required: true }]}><Select showSearch options={organizations.filter(item => item.status === 'active').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="role_type_id" label="角色" rules={[{ required: true }]}><Select options={roleTypes.filter(item => item.subject_type === 'organization').map(item => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="valid_from" label="生效时间"><Input placeholder="留空表示即时生效" /></Form.Item>
          </>}
          {modal === 'merge' && <>
            <Alert type="warning" showIcon message="只建议合并确认属于同一人的档案；身份证号不一致时后端会拒绝。" className="mb-3" />
            <Form.Item name="target_person_id" label="合并到目标档案" rules={[{ required: true }]}><Select showSearch options={people.filter(item => item.id !== selected?.id && item.status === 'active').map(item => ({ value: item.id, label: `${item.name}（${item.id}）` }))} /></Form.Item>
            <Form.Item name="reason" label="合并原因"><Input.TextArea autoSize /></Form.Item>
          </>}
        </Form>
      </Modal>

      <Drawer open={detailOpen} onClose={() => setDetailOpen(false)} width="min(96vw, 980px)" title={detail?.normalized_address || detail?.name || '档案详情'}>
        {!detail ? <div className="py-16 text-center text-[var(--app-text-secondary)]">正在读取…</div> : <div className="registry-detail">
          {canManage && detailKind === 'property' && <Space wrap>
            <Button onClick={() => openEdit('property', detail)}>编辑房屋</Button>
            <Button icon={<PlusOutlined />} onClick={() => { setSelected(detail); form.resetFields(); setModal('alias') }}>添加地址别名</Button>
            <Button onClick={() => { setSelected(detail); form.resetFields(); setModal('personRelation') }}>添加人员关系</Button>
            <Button onClick={() => { setSelected(detail); form.resetFields(); setModal('organizationRelation') }}>添加机构关系</Button>
          </Space>}
          {canManage && detailKind === 'person' && <Space><Button onClick={() => openEdit('person', detail)}>编辑人员</Button><Button onClick={() => { setSelected(detail); form.resetFields(); setModal('phone') }}>添加号码</Button></Space>}
          {canManage && detailKind === 'organization' && <Space><Button onClick={() => openEdit('organization', detail)}>编辑机构</Button></Space>}
          {detailKind === 'property' && detail.certificate_summary && (() => {
            const summary = detail.certificate_summary
            const current = detail.certificates?.[0]
            return <section className={`registry-certificate-summary registry-certificate-summary--${summary.certificate_status}`}>
              <div className="registry-certificate-summary__heading">
                <div>
                  <span className="registry-certificate-summary__eyebrow">房东责任告知书</span>
                  <strong>{summary.certificate_status_label}</strong>
                </div>
                <Tag color={certificateStatusColors[summary.certificate_status as Exclude<RegistryCertificateStatus, ''>] || 'default'}>
                  {summary.certificate_count ? `${summary.certificate_count} 份记录` : '暂无记录'}
                </Tag>
              </div>
              <div className="registry-certificate-summary__grid">
                <div><span>房东</span><strong>{current?.landlord_name || '未确定'}</strong></div>
                <div><span>实际出租人</span><strong>{current?.actual_renter_name || '未确定'}</strong></div>
                <div><span>责任关系</span><strong>{summary.landlord_renter_relation_label}</strong></div>
                <div><span>责任身份</span><strong>{summary.responsibility_identity || '未确认'}</strong></div>
              </div>
              {!['normal_signed', 'not_required', 'not_applicable'].includes(summary.certificate_status) && (
                <div className="registry-certificate-summary__action">
                  {summary.certificate_status === 'not_uploaded' && '需要补充房东责任告知书。'}
                  {summary.certificate_status === 'renter_needs_correction' && '告知书已经签署，但实际出租人信息需要修正。'}
                  {summary.certificate_status === 'actual_renter_missing' && '尚未确定实际承担出租管理责任的人。'}
                  {summary.certificate_status === 'multiple_or_conflict' && '告知书来源存在重复、内容冲突或地址匹配问题，需要先完成核对。'}
                </div>
              )}
              <div className="registry-certificate-summary__updated">
                最近来源读取：{summary.certificate_updated_at ? formatUTCTime(summary.certificate_updated_at, systemTimezone) : '暂无'}
              </div>
            </section>
          })()}
          {detailKind === 'property' && <section className="registry-visit-summary">
            <div>
              <span>最近一次走访</span>
              <strong>{detail.latest_visit_date || '暂无走访记录'}</strong>
            </div>
            <div>
              <span>历史走访</span>
              <strong>{detail.visit_count || 0} 次</strong>
            </div>
            <div>
              <span>最近星级评定</span>
              <strong>{detail.latest_star_rating || '暂无评定'}</strong>
            </div>
          </section>}
          {detailKind === 'property' && (() => {
            const match = detail.small_community_match || {}
            const status = addressMatchStatusView(match.status || 'unmatched')
            return <section className="registry-small-community-match">
              <div className="registry-small-community-match__heading">
                <div>
                  <span>房屋所属小区</span>
                  <strong>{match.small_community_name || '尚未关联小区'}</strong>
                </div>
                <Tag color={status.color}>{status.label}</Tag>
              </div>
              <div className="registry-small-community-match__facts">
                <div><span>所属社区</span><strong>{match.community_name || detail.community_name || '未确定'}</strong></div>
                <div><span>匹配方式</span><strong>{match.method || '尚未匹配'}</strong></div>
                <div><span>匹配分数</span><strong>{Math.round((match.score || 0) * 100)} 分</strong></div>
              </div>
              <p>{match.reason || '规则匹配只生成建议，确认前不会用于任务分配。'}</p>
              {Array.isArray(match.candidates) && match.candidates.length > 0 && <div className="registry-small-community-match__candidates">
                <span>候选小区</span>
                <Space size={[6, 6]} wrap>
                  {match.candidates.slice(0, 5).map((candidate: any) => (
                    <Tag key={`${candidate.entry_id}-${candidate.community_id || 0}`}>
                      {candidate.name} · {candidate.community_name || '社区未确定'} · {Math.round((candidate.score || 0) * 100)} 分
                    </Tag>
                  ))}
                </Space>
              </div>}
              {canManage && <Button size="small" onClick={() => {
                const property = properties.find(item => item.id === detail.id) || {
                  ...detail,
                  small_community_id: match.small_community_id,
                  address_match_status: match.status,
                  address_match_reason: match.reason,
                }
                setMatchConfirmProperty(property)
                setMatchConfirmEntryId(match.small_community_id || undefined)
              }}>{match.status === 'confirmed' ? '修正小区' : '人工确认小区'}</Button>}
            </section>
          })()}
          <Descriptions bordered size="small" column={1} items={Object.entries(detail).filter(([key, value]) => !Array.isArray(value) && typeof value !== 'object' && !['identity_hmac', 'certificate_summary', 'visit_count', 'latest_visit_date', 'latest_star_rating', 'latest_star_rating_at', 'small_community_match'].includes(key)).slice(0, 12).map(([key, value]) => ({ key, label: key, children: String(value ?? '-') }))} />
          {detailKind === 'person' && canViewTags && <Panel
            title="人员标签"
            extra={canManageTags ? <Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openCreate('personTag')}>添加标签</Button> : undefined}
          >
            <Space direction="vertical" className="w-full" size="middle">
              <Space size={[4, 4]} wrap>
                {(detail.categories || []).length
                  ? detail.categories.map((item: any) => <Tag key={item.assignment_id} color={item.color}>{item.name}</Tag>)
                  : <span className="text-[var(--app-text-secondary)]">暂无有效标签</span>}
              </Space>
              {(detail.tag_assignments || []).length > 0 && <AppTable
                rowKey="assignment_id"
                size="small"
                pagination={false}
                dataSource={detail.tag_assignments}
                columns={[
                  { title: '标签', dataIndex: 'category_name', render: (value: string, row: any) => <Tag color={row.color}>{value}</Tag> },
                  { title: '生效时间', dataIndex: 'valid_from', width: 170, render: value => value ? formatUTCTime(value, systemTimezone) : '-' },
                  { title: '结束/解除', width: 170, render: (_: unknown, row: any) => row.released_at || row.valid_to ? formatUTCTime(row.released_at || row.valid_to, systemTimezone) : '持续有效' },
                  { title: '状态', dataIndex: 'status', width: 90, render: value => value === 'active' ? <Tag color="green">有效</Tag> : <Tag>{value}</Tag> },
                  { title: '依据', dataIndex: 'basis', ellipsis: true },
                  { title: '操作', width: 90, render: (_: unknown, row: any) => canManageTags && row.status === 'active' && !row.released_at && <Popconfirm title="确认解除该标签？" onConfirm={() => void releasePersonTag(row)}><Button type="link" size="small">解除</Button></Popconfirm> },
                ]}
                scroll={{ x: 800 }}
              />}
            </Space>
          </Panel>}
          {detailKind === 'property' && <Panel title="历史走访与星级评定" extra={<Tag color={propertyVisitTotal ? 'blue' : 'default'}>{propertyVisitTotal} 次走访</Tag>}>
            <AppTable
              rowKey="id"
              loading={propertyVisitLoading}
              dataSource={propertyVisits}
              scroll={{ x: 980 }}
              emptyText="当前房屋暂无可关联的走访记录"
              pagination={{
                current: propertyVisitPage,
                pageSize: 20,
                total: propertyVisitTotal,
                showSizeChanger: false,
                onChange: nextPage => void loadPropertyVisits(nextPage),
              }}
              columns={[
                { title: '走访日期', dataIndex: 'business_date', width: 120, responsivePriority: 'always' },
                { title: '走访时间', dataIndex: 'visited_at', width: 170, responsivePriority: 'standard', render: value => value ? formatUTCTime(value, systemTimezone) : '-' },
                { title: '走访人', dataIndex: 'operator_name', width: 110, responsivePriority: 'always' },
                { title: '进入方式', dataIndex: 'entry_method', width: 90, responsivePriority: 'wide' },
                { title: '房间核查', dataIndex: 'room_check_count', width: 100, responsivePriority: 'standard' },
                { title: '变动', width: 170, responsivePriority: 'wide', render: (_, row) => `新增 ${row.added_count} · 变更 ${row.changed_count} · 注销 ${row.cancelled_count}` },
                { title: '星级评定', dataIndex: 'star_rating', width: 130, responsivePriority: 'always', render: value => value ? <Tag color="gold">{value}</Tag> : '-' },
                { title: '得分', dataIndex: 'score', width: 90, responsivePriority: 'standard', render: value => value ?? '-' },
                { title: '走访地址', dataIndex: 'address', width: 260, ellipsis: true, responsivePriority: 'wide' },
              ] as ResponsiveColumns<RegistryPropertyVisit>}
            />
          </Panel>}
          {detail.aliases && <Panel title="地址别名"><Space wrap>{detail.aliases.length ? detail.aliases.map((item: any) => <Tag key={item.id} color={item.enabled ? 'blue' : undefined}>{item.alias}{item.enabled ? '' : ' · 已停用'}{canManage && <Button type="link" size="small" onClick={async () => { await registryApi.changeAliasStatus(item.id, { status: item.enabled ? 'inactive' : 'active' }); setDetail(await registryApi.property(detail.id)) }}>{item.enabled ? '停用' : '启用'}</Button>}</Tag>) : '暂无'}</Space></Panel>}
          {detail.phones && <Panel title="联系电话" extra={canManage ? <Button size="small" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setModal('phone') }}>添加号码</Button> : undefined}>
            <Space wrap>{detail.phones.length ? detail.phones.map((item: any) => <Tag key={item.id} color={item.is_primary ? 'blue' : undefined}>{item.phone}{item.is_primary ? ' · 主号码' : ''}</Tag>) : '暂无'}</Space>
          </Panel>}
          {detail.people && <Panel title="房屋人员关系"><AppTable rowKey="relation_id" pagination={false} dataSource={detail.people} columns={[{ title: '姓名', dataIndex: 'person_name' }, { title: '角色', dataIndex: 'role_name' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }, { title: '操作', render: (_: unknown, row: any) => canManage && !row.valid_to && <Popconfirm title="结束该关系？" onConfirm={() => void endRelation('person', row)}><Button type="link" size="small">结束</Button></Popconfirm> }]} /></Panel>}
          {detail.organizations && <Panel title="房屋机构关系"><AppTable rowKey="relation_id" pagination={false} dataSource={detail.organizations} columns={[{ title: '机构', dataIndex: 'organization_name' }, { title: '角色', dataIndex: 'role_name' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }, { title: '操作', render: (_: unknown, row: any) => canManage && !row.valid_to && <Popconfirm title="结束该关系？" onConfirm={() => void endRelation('organization', row)}><Button type="link" size="small">结束</Button></Popconfirm> }]} /></Panel>}
          {detail.members && <Panel title="机构经办人"><AppTable rowKey="membership_id" pagination={false} dataSource={detail.members} columns={[{ title: '姓名', dataIndex: 'person_name' }, { title: '职位', dataIndex: 'title' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }, { title: '操作', render: (_: unknown, row: any) => canManage && !row.valid_to && <Popconfirm title="结束该任职关系？" onConfirm={() => void endRelation('membership', row)}><Button type="link" size="small">结束</Button></Popconfirm> }]} /></Panel>}
          {detail.properties && <Panel title="机构关联房屋"><AppTable rowKey="relation_id" pagination={false} dataSource={detail.properties} columns={[{ title: '地址', dataIndex: 'normalized_address' }, { title: '角色', dataIndex: 'role_name' }, { title: '生效', dataIndex: 'valid_from' }, { title: '结束', dataIndex: 'valid_to' }]} /></Panel>}
          {detail.certificates && <Panel title="房东责任告知书"><AppTable rowKey="id" pagination={false} dataSource={detail.certificates} columns={[
            { title: '房东', dataIndex: 'landlord_name', width: 130 },
            { title: '实际出租人', dataIndex: 'actual_renter_name', width: 130 },
            { title: '签署状态', dataIndex: 'signed_status', width: 110 },
            { title: '签署类型', dataIndex: 'sign_type', width: 130 },
            { title: '签署时间', dataIndex: 'sign_time', width: 180, render: value => value ? formatUTCTime(value, systemTimezone) : '-' },
            { title: '最近读取', dataIndex: 'source_last_seen_at', width: 180, render: value => value ? formatUTCTime(value, systemTimezone) : '-' },
            {
              title: '告知书图片',
              dataIndex: 'has_image',
              width: 170,
              render: (hasImage, certificate: any) => {
                if (!hasImage) return <span className="text-[var(--app-text-secondary)]">来源未提供图片</span>
                if (!canReview) return <span className="text-[var(--app-text-secondary)]">需档案导入权限</span>
                return <Button type="link" icon={<FileImageOutlined />} loading={certificateImageLoading === certificate.id} onClick={() => void openCertificateImage(certificate)}>查看责任告知书</Button>
              },
            },
          ]} /></Panel>}
          {detail.versions && <Panel title="地址版本历史"><AppTable rowKey="version" pagination={false} dataSource={detail.versions} columns={[{ title: '版本', dataIndex: 'version', width: 80 }, { title: '标准化地址', dataIndex: 'normalized_address' }, { title: '变更原因', dataIndex: 'reason' }, { title: '时间', dataIndex: 'created_at', width: 180, render: value => formatUTCTime(value, systemTimezone) }]} /></Panel>}
        </div>}
      </Drawer>
      <Modal
        open={Boolean(certificatePreview)}
        title={certificatePreview?.title || '责任告知书'}
        width={900}
        footer={<Button type="primary" onClick={() => setCertificatePreview(null)}>关闭</Button>}
        onCancel={() => setCertificatePreview(null)}
        destroyOnClose
      >
        {certificatePreview && (
          <div className="flex justify-center overflow-auto bg-[var(--app-surface-muted)] p-3">
            <Image src={certificatePreview.url} preview={false} alt="房东责任告知书" className="max-h-[70vh] object-contain" />
          </div>
        )}
      </Modal>
    </div>
  )
}
