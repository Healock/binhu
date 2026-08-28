import type { ImgHTMLAttributes, ReactNode } from 'react'
import { Avatar, Skeleton, type AvatarProps } from 'antd'
import useAuthenticatedImageUrl from '../hooks/useAuthenticatedImageUrl'

type AuthenticatedAvatarProps = Omit<AvatarProps, 'src'> & {
  src?: string | null
}

export function AuthenticatedAvatar({ src, ...props }: AuthenticatedAvatarProps) {
  const image = useAuthenticatedImageUrl(src)
  return <Avatar {...props} src={image.url} />
}

interface AuthenticatedImageProps extends Omit<ImgHTMLAttributes<HTMLImageElement>, 'src'> {
  src?: string | null
  fallback?: ReactNode
}

export default function AuthenticatedImage({ src, alt = '', fallback, ...props }: AuthenticatedImageProps) {
  const image = useAuthenticatedImageUrl(src)
  if (image.loading) {
    return <Skeleton.Image active aria-label={`${alt || '图片'}加载中`} />
  }
  if (!image.url) {
    return <>{fallback || <span className="text-sm text-[var(--app-text-secondary)]">{alt || '图片'}加载失败</span>}</>
  }
  return <img {...props} src={image.url} alt={alt} />
}
