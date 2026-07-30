import type { ComponentProps } from 'react'
import {
  ApartmentOutlined,
  AppstoreOutlined,
  BarChartOutlined,
  DatabaseOutlined,
  FolderOpenOutlined,
  FileWordOutlined,
  MonitorOutlined,
  ReadOutlined,
  SearchOutlined,
  SettingOutlined,
  TeamOutlined,
  UploadOutlined,
  UserOutlined,
} from '@ant-design/icons'
import type { NavigationIconName } from '../navigation/mobileNavigation'

const ICONS = {
  workspace: AppstoreOutlined,
  resources: FolderOpenOutlined,
  system: SettingOutlined,
  summary: BarChartOutlined,
  query: SearchOutlined,
  visit: ReadOutlined,
  upload: UploadOutlined,
  worklog: FileWordOutlined,
  members: TeamOutlined,
  communities: ApartmentOutlined,
  users: UserOutlined,
  settings: SettingOutlined,
  operations: MonitorOutlined,
} satisfies Record<
  NavigationIconName,
  React.ComponentType<ComponentProps<'span'>>
>

export default function NavigationIcon({
  name,
  className,
}: {
  name: NavigationIconName
  className?: string
}) {
  const Icon = ICONS[name] || DatabaseOutlined
  return <Icon className={className} />
}
