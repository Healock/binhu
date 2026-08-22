import { Button } from 'antd'
import { ArrowLeftOutlined, ToolOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'

export default function OfflineMode() {
  const navigate = useNavigate()

  return (
    <main className="min-h-screen bg-[var(--app-page-bg)] px-6 py-10 text-[var(--app-text-primary)]">
      <div className="mx-auto max-w-3xl">
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={() => navigate('/login')}
        >
          返回登录
        </Button>
        <section className="app-card mt-6 p-8">
          <div className="flex items-start gap-4">
            <ToolOutlined className="mt-1 text-2xl text-[var(--app-primary)]" />
            <div>
              <h1 className="m-0 text-2xl font-semibold">离线模式</h1>
              <p className="mt-3 text-[var(--app-text-secondary)]">
                这里将提供无需连接服务器即可使用的本地工具。当前版本先完成入口和本地运行环境，具体工具将按工作流程逐步加入。
              </p>
            </div>
          </div>
        </section>
      </div>
    </main>
  )
}
