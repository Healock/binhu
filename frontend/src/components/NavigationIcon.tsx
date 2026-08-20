import {
  IconBrain,
  IconBuildingCommunity,
  IconBuildings,
  IconChartBar,
  IconClipboardCheck,
  IconCloudUpload,
  IconDatabase,
  IconDatabaseSearch,
  IconFileDescription,
  IconFolder,
  IconHomeSearch,
  IconLayoutDashboard,
  IconLayoutGrid,
  IconListCheck,
  IconPhotoSearch,
  IconQrcode,
  IconRoute,
  IconSend,
  IconServerCog,
  IconSettings,
  IconSettingsAutomation,
  IconShieldLock,
  IconTags,
  IconTicket,
  IconUserCog,
  IconUsers,
  IconWalk,
  type TablerIcon,
} from '@tabler/icons-react'
import type { NavigationIconName } from '../navigation/mobileNavigation'

const ICONS = {
  dashboard: IconLayoutDashboard,
  workspace: IconLayoutGrid,
  task_flow: IconRoute,
  data_query: IconDatabaseSearch,
  data_upload: IconCloudUpload,
  file_generation: IconFileDescription,
  ticket_center: IconTicket,
  tasks: IconClipboardCheck,
  online_check: IconListCheck,
  dispatch: IconSend,
  analysis: IconBrain,
  photo: IconPhotoSearch,
  resources: IconFolder,
  system: IconSettings,
  summary: IconChartBar,
  visit: IconWalk,
  code_summary: IconQrcode,
  worklog: IconSettingsAutomation,
  members: IconUsers,
  communities: IconBuildingCommunity,
  neighborhoods: IconBuildings,
  registry: IconHomeSearch,
  tags: IconTags,
  users: IconUserCog,
  permissions: IconShieldLock,
  settings: IconSettings,
  operations: IconServerCog,
} satisfies Record<NavigationIconName, TablerIcon>

export default function NavigationIcon({
  name,
  className,
}: {
  name: NavigationIconName
  className?: string
}) {
  const Icon = ICONS[name] || IconDatabase
  return (
    <Icon
      aria-hidden="true"
      className={className}
      focusable="false"
      size="1em"
      stroke={1.8}
    />
  )
}
