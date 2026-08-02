import { useEffect, useState } from 'react'

const MOBILE_QUERY = '(max-width: 767px)'

export default function useMobileViewport(): boolean {
  const [mobile, setMobile] = useState(() => (
    typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches
  ))

  useEffect(() => {
    const media = window.matchMedia(MOBILE_QUERY)
    const update = () => setMobile(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  return mobile
}
